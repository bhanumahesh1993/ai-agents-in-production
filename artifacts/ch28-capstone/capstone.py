"""The whole system in one place, composed from the shared packages.

Six planes, wired the way Chapter 28 reads them, and nothing here
reimplements anything: every part comes from ``packages/``.

============  ============================================================
plane         what it is here
============  ============================================================
experience    ``demo.py`` and the approval inbox rendered from
              ``ApprovalStore.pending()``
admission     ``admission.AdmissionLayer`` — identity, risk, budgets,
              version pinning, the durable run record
run           ``northstar_runtime.DurableRunner`` over ``AgentLoop``,
              with a journal and a checkpointer
intelligence  ``FakeModel`` and ``FlakyModel``, pinned by a snapshot name
              that rides in the configuration hash
action        ``ToolRegistry`` with derived idempotency keys, behind the
              policy decision point and the approval gate
data          ``World`` — orders, refunds, messages, and the append-only
              side-effect ledger that settles what happened
control       ``Instrumentation`` and ``CostLedger`` for evidence,
              ``StateGrader`` and ``TrajectoryGrader`` for verdicts
============  ============================================================

The claim this file makes is that a system with this much machinery in it
still runs end to end on a laptop with no API key. If it did not, the
coupling would have gone wrong somewhere.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from admission import Admission, AdmissionLayer, Ticket
from northstar_contracts import Money, RunState, World, content_hash
from northstar_contracts import idempotency_key as northstar_key
from northstar_evals import GradeResult, grade_all
from northstar_policy import (
    ApprovalRequest,
    ApprovalStore,
    BudgetExceeded,
    Decision,
    RulesPolicyEngine,
    default_northstar_policy,
    require_scope,
)
from northstar_runtime import (
    DurableRunner,
    FakeModel,
    FlakyModel,
    MemoryCheckpointer,
    MemoryJournal,
    ModelProvider,
    PolicyDenied,
    SimulatedCrash,
    ToolRegistry,
)
from northstar_telemetry import (
    CostLedger,
    Instrumentation,
    ModelPrice,
    Redactor,
    SpanRecorder,
)

__all__ = ["CaseResult", "Capstone", "capstone_policy"]

#: Illustrative. The mock model is free, which is true and unhelpful for a
#: cost report, so the capstone prices it at a placeholder rate and says
#: so everywhere the number appears.
ILLUSTRATIVE_PRICE = ModelPrice(
    300, 1500, note="ILLUSTRATIVE PLACEHOLDER - not a provider's rate"
)


def capstone_policy() -> RulesPolicyEngine:
    """The book's policy, with a scope rule on every write.

    ``default_northstar_policy`` already requires ``refunds:write`` for
    money, gates fraud-flagged orders, and gates refunds at or above the
    5,000-cent threshold. The two rules added here make the remaining
    scopes granted at admission mean something rather than being
    decoration.
    """
    engine = default_northstar_policy()
    engine.rules.insert(0, require_scope("send_message", "messages:write"))
    engine.rules.insert(
        0, require_scope("escalate_to_specialist", "cases:write")
    )
    return engine


@dataclass
class CaseResult:
    """One ticket, handled: the outcome, the evidence, and the cost.

    Everything an incident review, a release gate, or an auditor would ask
    for about a single run, gathered in one value.
    """

    admission: Admission
    state: RunState
    world: World
    grade: GradeResult
    approvals: list[ApprovalRequest] = field(default_factory=list)
    journal: list[dict[str, Any]] = field(default_factory=list)
    spans: list[Any] = field(default_factory=list)
    cost_cents: Money = 0
    replayed_effects: int = 0
    executed_effects: int = 0
    crashed: bool = False
    resumed: bool = False
    error: str = ""
    notes: list[str] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        """Whether the authoritative world says this ticket was handled."""
        return self.grade.passed

    @property
    def refunds(self) -> int:
        """Refund rows in the world. Two is the incident."""
        return len(self.world.refunds)

    @property
    def mutations(self) -> int:
        """Entries in the side-effect ledger."""
        return len(self.world.ledger)

    @property
    def has_evidence(self) -> bool:
        """Whether this run could be reconstructed end to end.

        A configuration hash, a journal, a trace, and a graded verdict. A
        high-risk run you cannot reconstruct is a defect in its own right,
        independent of whether it succeeded.
        """
        return bool(
            self.admission.config_hash
            and self.journal
            and self.spans
            and self.grade.details
        )

    def summary(self) -> dict[str, Any]:
        """The row a dashboard or a go-live pack would show."""
        return {
            "run_id": self.admission.run_id,
            "risk": self.admission.risk,
            "config": self.admission.short_config_hash,
            "status": self.state.status,
            "passed": self.passed,
            "refunds": self.refunds,
            "mutations": self.mutations,
            "approvals": len(self.approvals),
            "journal_records": len(self.journal),
            "spans": len(self.spans),
            "cost_cents": self.cost_cents,
            "evidence_complete": self.has_evidence,
        }


class Capstone:
    """The composed Northstar system, assembled per case.

    Args:
        idempotency: Stamp every write with a key derived from
            ``(run_id, step, call_id)``. On by default. Turning it off is
            how the demo shows what the key is holding up.
        seed: When given, the model drifts — repeats, stalls, and gives up
            — from a seeded generator, so the graded suite measures
            something other than a script.
        drift: Total probability that a turn goes wrong. Split evenly
            across the three failure modes from Chapter 16.
    """

    def __init__(
        self,
        *,
        idempotency: bool = True,
        seed: int | None = None,
        drift: float = 0.0,
    ) -> None:
        self.world = World()
        self.admission = AdmissionLayer(self.world)
        self.approvals = ApprovalStore()
        self.policy = capstone_policy()
        self.journal = MemoryJournal()
        self.checkpointer = MemoryCheckpointer()
        self.ledger = CostLedger()
        self.ledger.register("fake-model-1", ILLUSTRATIVE_PRICE)
        self.ledger.register("flaky-model-1", ILLUSTRATIVE_PRICE)
        self.telemetry = Instrumentation(
            SpanRecorder(), redactor=Redactor.default(), ledger=self.ledger
        )
        self.idempotency = idempotency
        self.seed = seed
        self.drift = drift
        self.runner: DurableRunner | None = None

    # ------------------------------------------------------------ assembly

    def _model(self, script: list[Any]) -> ModelProvider:
        """The intelligence plane: scripted, and optionally drifting."""
        base = FakeModel(default=script, strict=False)
        if self.seed is None or self.drift <= 0:
            return base
        share = self.drift / 3.0
        return FlakyModel(
            base,
            seed=self.seed,
            p_repeat=share,
            p_stall=share,
            p_giveup=share,
        )

    def _message_tool(self, run_id: str) -> Any:
        """``send_message``, keyed by content rather than by step.

        The runtime's default key is derived from ``(run_id, step,
        call_id)``, which is right for a refund: two refunds at two steps
        are two intents, and the second is a decision the agent made
        rather than a retry of the first. It is wrong for an apology. The
        same message twice in one run is never an intent, and unlike a
        refund it cannot be clawed back — so this tool derives its key
        from ``(run_id, content)`` and the second send collapses into the
        first even when the model repeats itself several turns later.

        Which derivation a tool wants is a property of the tool contract,
        which is why it is decided here rather than in the loop.
        """
        world = self.world

        def send_message(
            order_id: str,
            body: str,
            channel: str = "email",
            idempotency_key: str | None = None,
        ) -> dict[str, Any]:
            derived = northstar_key(
                run_id,
                content_hash(
                    {"order_id": order_id, "body": body, "channel": channel}
                ),
            )
            return world.send_message(
                order_id, body, channel, idempotency_key=derived
            )

        return send_message

    def _runner(self, admission: Admission, script: list[Any]) -> DurableRunner:
        """The run plane, with every other plane hung off it."""
        bindings = [
            (
                spec,
                self._message_tool(admission.run_id)
                if (spec.name == "send_message" and self.idempotency)
                else fn,
            )
            for spec, fn in self.world.tools()
        ]
        tools = ToolRegistry(
            inject_idempotency_key=self.idempotency
        ).register_all(bindings)
        runner = DurableRunner(
            model=self._model(script),
            tools=tools,
            journal=self.journal,
            checkpointer=self.checkpointer,
            policy=self.policy,
            approvals=self.approvals,
            principal=admission.principal,
            telemetry=self.telemetry,
            max_turns=admission.max_turns,
            budget_cents=admission.budget_cents,
            idempotency=self.idempotency,
        )
        self.runner = runner
        return runner

    # --------------------------------------------------------------- cases

    def handle(
        self,
        ticket: Ticket,
        script: list[Any],
        graders: list[Any],
        *,
        crash_after_step: int | None = None,
        approve_by: str | None = None,
        fault: str | None = None,
    ) -> CaseResult:
        """Admit one ticket, run it, recover it if it dies, and grade it.

        Args:
            ticket: The inbound request.
            script: What the model does, turn by turn.
            graders: Read the authoritative world and the trajectory.
            crash_after_step: Kill the worker once this step has
                committed, then resume from the journal. The resume is
                part of the case, not a separate exercise.
            approve_by: Who decides a pending approval. ``None`` leaves
                the run suspended, which is the honest default: an
                approval nobody answers is a run that stays waiting.
            fault: Injected into ``issue_refund``. ``"timeout"`` commits
                the write and then raises, which is the only failure a
                caller cannot interpret.

        Returns:
            A :class:`CaseResult` holding the outcome and the evidence.
        """
        admitted = self.admission.admit(ticket)
        notes: list[str] = []
        if not admitted.admitted:
            return CaseResult(
                admission=admitted,
                state=RunState(run_id=admitted.run_id, status="failed"),
                world=self.world,
                grade=GradeResult(False, 0.0, [admitted.reason], "admission"),
                notes=[admitted.reason],
            )

        if fault:
            self.world.inject_fault("issue_refund", kind=fault)

        runner = self._runner(admitted, script)
        crashed = resumed = False
        error = ""

        try:
            state = runner.start(
                ticket.text,
                run_id=admitted.run_id,
                crash_after_step=crash_after_step,
            )
        except SimulatedCrash as exc:
            crashed = True
            notes.append(str(exc))
            state = runner.replay(admitted.run_id)
            notes.append(
                f"replayed to step {state.step} without re-executing "
                f"anything"
            )
            state = runner.resume(admitted.run_id)
            resumed = True
        except (BudgetExceeded, PolicyDenied) as exc:
            error = f"{type(exc).__name__}: {exc}"
            notes.append(error)
            state = RunState(run_id=admitted.run_id, status="failed")

        pending = list(self.approvals.pending())
        if state.status == "waiting_approval" and approve_by and pending:
            request = pending[0]
            notes.append(
                f"approval {request.id} bound to fingerprint "
                f"{request.fingerprint[:12]}"
            )
            try:
                state = runner.approve(
                    admitted.run_id, request.id, by=approve_by
                )
                resumed = True
            except (BudgetExceeded, PolicyDenied) as exc:
                error = f"{type(exc).__name__}: {exc}"
                notes.append(error)

        self.admission.release()
        replay_tools = getattr(runner.last_loop, "tools", None)
        return CaseResult(
            admission=admitted,
            state=state,
            world=self.world,
            grade=grade_all(graders, state, self.world),
            approvals=pending,
            journal=runner.history(admitted.run_id),
            spans=list(self.telemetry.spans),
            cost_cents=self.ledger.per_run_cents(admitted.run_id),
            replayed_effects=getattr(replay_tools, "replayed", 0),
            executed_effects=getattr(replay_tools, "executed", 0),
            crashed=crashed,
            resumed=resumed,
            error=error,
            notes=notes,
        )

    # ------------------------------------------------------------ evidence

    def inbox(self) -> list[dict[str, Any]]:
        """What a human reviewing a pending action would see.

        The whole payload, not a paraphrase. An approval flow that renders
        a summary is approval theatre with a cryptographic hash bolted on.
        """
        return [request.to_dict() for request in self.approvals.pending()]

    def would_be_approved(self, call: Any, run_id: str) -> bool:
        """Whether an exact call is currently cleared to run."""
        return self.approvals.is_approved(call, run_id)

    def policy_decision(self, principal: Any, call: Any) -> Decision:
        """What the policy decision point says about one proposed call."""
        return self.policy.evaluate(principal, call, {})
