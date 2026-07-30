"""The workflow, and the pause that returns instead of raising.

The pause is the least dramatic code in the artifact, which is the point.

Three details carry the design. The checkpoint is written before the
function returns, so a crash between the policy decision and the return
loses nothing. Nothing raises, so no handler has to guess what was
half-done. And the function hands the run back to the caller, releasing the
worker: the run now costs storage and nothing else for as long as it waits.

The one deviation from the chapter's excerpt is that ``RunState`` is
frozen, so ``state.status = "waiting_approval"`` is spelled
``state.with_status("waiting_approval")``. A run's history is evidence, and
evidence you can edit in place is not evidence.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from approvals import DurableApprovals
from envelope import SCHEMA_VERSION, Envelope, EnvelopeStore, config_hash
from keys import key_for
from northstar_contracts import (
    Message,
    RunState,
    ToolCall,
    World,
)
from northstar_policy import Decision, PolicyEngine, Principal
from northstar_runtime import SimulatedCrash
from side_effects import RefundService, SideEffectLedger
from states import RunPhase

__all__ = [
    "AMOUNT_CENTS",
    "APPROVAL_QUEUE",
    "ORDER",
    "RUN_ID",
    "STEP_NOTICE",
    "STEP_REFUND",
    "Workflow",
    "WorkflowConfig",
]

#: Order NR-2026-0042110, US$240.00, flagged for fraud review.
ORDER = "NR-2026-0042110"
#: The whole order back, well above the 5,000-cent threshold.
AMOUNT_CENTS = 24_000
#: A queue, not a person. The opening incident cost sixty-one hours
#: because a request was addressed to someone on leave.
APPROVAL_QUEUE = "fraud-review"
RUN_ID = "run_01HQ8ZK3M7NR4T2VXBW9YF6PDA"

#: Journal positions. Durable and monotonic by construction, which is what
#: makes a derived key stable across a resume. Not an in-memory counter,
#: not a position in a message list, not a retry count.
STEP_NOTICE = 1
STEP_REFUND = 2


@dataclass(frozen=True)
class WorkflowConfig:
    """Everything that decides how this worker behaves.

    The fields are the ones the configuration hash covers, plus the two
    the demo flips to reproduce a failure.
    """

    agent_version: str
    key_strategy: str = "derived"
    kill_after_settle: bool = False
    system_prompt: str = "northstar-support/v1"
    policy_bundle: str = "northstar-refunds/2026-07"
    model: str = "fake-model-1"
    tool_versions: dict[str, str] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.tool_versions is None:
            object.__setattr__(
                self,
                "tool_versions",
                {"issue_refund": "3", "send_message": "1"},
            )

    @property
    def hash(self) -> str:
        """The effective configuration hash that rides in the envelope."""
        return config_hash(
            self.agent_version,
            self.system_prompt,
            self.tool_versions,
            self.policy_bundle,
            self.model,
        )


class Workflow:
    """The Northstar refund workflow, written to survive its process.

    Args:
        store: Envelope store, holding the checkpoint and the transitions.
        approvals: Durable approval inbox.
        ledger: The intent journal you own.
        service: The payment provider stand-in.
        policy: Decision point consulted before every consequential call.
        principal: Who this run acts as.
        config: This worker's version and failure-injection switches.
        world: Read tools. The writes go through ``service``, because a
            world in memory cannot answer a question asked three days later
            by a different process.
    """

    def __init__(
        self,
        store: EnvelopeStore,
        approvals: DurableApprovals,
        ledger: SideEffectLedger,
        service: RefundService,
        policy: PolicyEngine,
        principal: Principal,
        config: WorkflowConfig,
        world: World | None = None,
    ) -> None:
        self.store = store
        self.approvals = approvals
        self.ledger = ledger
        self.service = service
        self.policy = policy
        self.principal = principal
        self.config = config
        self.world = world or World()

    # ------------------------------------------------------------ the pause

    def step(self, envelope: Envelope, call: ToolCall) -> Envelope:
        """Take one consequential step, or park the run in front of it.

        Runs as the support-agent principal, scope ``refunds:write``.
        """
        decision = self.policy.evaluate(self.principal, call, {})
        if decision is Decision.REQUIRE_APPROVAL:
            request = self.approvals.request(
                run_id=envelope.state.run_id,
                step_id=envelope.state.step,
                call=call,
                reason=(
                    f"{call.arguments.get('amount_cents')}c is at or above "
                    f"the 5000c threshold on a fraud-flagged order"
                ),
                principal=self.principal,
                queue=APPROVAL_QUEUE,
            )
            envelope.pending_call = call.to_dict()
            # Frozen state, so this is `with_status` rather than an
            # assignment. The checkpoint is written before we return.
            self.store.transition(
                envelope,
                RunPhase.WAITING_APPROVAL,
                actor=f"worker:{self.config.agent_version}",
                reason=f"approval.requested {request.id}",
            )
            return envelope                    # no lease, no compute
        if decision is Decision.DENY:
            self.store.transition(
                envelope,
                RunPhase.FAILED,
                actor=f"worker:{self.config.agent_version}",
                reason=f"policy denied {call.name}",
            )
            return envelope
        return self.dispatch(envelope, call)

    # --------------------------------------------------------- the dispatch

    def dispatch(
        self,
        envelope: Envelope,
        call: ToolCall,
        key: str | None = None,
    ) -> Envelope:
        """Journal the intent, make the call, journal the outcome.

        The two journal writes are the whole resume story, and the gap
        between them is the ambiguity window from Chapter 1: the call may
        have committed while the response was lost.

        The ``generated`` key strategy differs in exactly one argument.
        Its key is a nonce created at call time, and the journal write that
        would have recorded it is not flushed before the call -- which is
        the window the chapter says a generated key introduces, expressed
        as a keyword argument.

        Args:
            key: The key this attempt presents. Supplied by
                :func:`resume.resume`, which computed it already and must
                not be handed a second, different one. Omitted on the first
                pass, where this method derives it.
        """
        step_id = envelope.state.step
        derived = self.config.key_strategy == "derived"
        if key is None:
            key = key_for(
                envelope.state.run_id, step_id, self.config.key_strategy
            )

        self.ledger.record_intent(
            key=key,
            run_id=envelope.state.run_id,
            step_id=step_id,
            tool=call.name,
            arguments=call.arguments,
            at=float(step_id),
            flush=derived,
        )
        outcome = self._perform(call, key)

        if self.config.kill_after_settle and call.name == "issue_refund":
            # The worker dies here: the effect has landed and nothing has
            # recorded that it did. With a derived key the next worker can
            # recompute it and ask. With a generated key the write above
            # was never flushed, so the key is gone.
            self.ledger.abandon()
            self.service.close()
            raise SimulatedCrash(
                f"worker killed after {call.name} settled, before the "
                f"outcome record landed"
            )

        self.ledger.record_outcome(key, outcome)
        envelope.state = envelope.state.with_messages(
            Message(role="tool", content={"tool": call.name, **outcome})
        ).advance()
        envelope.pending_call = None
        self.store.save(envelope)
        return envelope

    def present(self, call: ToolCall, key: str) -> dict[str, Any]:
        """Present one call to the system that owns its effect, under ``key``.

        A resume replays the step before the pause, so it presents that
        intent a second time. With a derived key the target recognises it
        and nothing happens twice. With a nonce it is a new intent, and the
        customer reads the same notice on Monday that they read on Friday.
        """
        return self._perform(call, key)

    def _perform(self, call: ToolCall, key: str) -> dict[str, Any]:
        """Route one call to the system that actually owns the effect."""
        if call.name == "issue_refund":
            return self.service.settle(
                kind="refund",
                order_id=str(call.arguments["order_id"]),
                amount_cents=int(call.arguments["amount_cents"]),
                reason=str(call.arguments.get("reason", "fraud_review")),
                idempotency_key=key,
            )
        if call.name == "send_message":
            return self.service.settle(
                kind="message",
                order_id=str(call.arguments["order_id"]),
                amount_cents=0,
                reason=str(call.arguments.get("body", ""))[:60],
                idempotency_key=key,
            )
        raise ValueError(f"{call.name} is not a consequential call here")

    # ------------------------------------------------------------- starting

    def start(self) -> Envelope:
        """Admit the run, send the notice, and reach the refund.

        Returns:
            The envelope, parked in ``waiting_approval`` in front of a
            24,000-cent refund on a fraud-flagged order.
        """
        order = self.world.get_order(ORDER)
        envelope = Envelope(
            state=RunState(
                run_id=RUN_ID,
                step=STEP_NOTICE,
                messages=[
                    Message(
                        role="user",
                        content=(
                            f"Customer disputes order {ORDER} and wants "
                            f"{order['total_cents']} cents back."
                        ),
                    )
                ],
            ),
            agent_version=self.config.agent_version,
            schema_version=SCHEMA_VERSION,
            config_hash=self.config.hash,
            phase=RunPhase.QUEUED,
            deadline_at=7 * 24 * 3600.0,
            principal=self.principal.to_dict(),
        )
        self.store.save(envelope)
        self.store.transition(
            envelope,
            RunPhase.RUNNING,
            actor=f"worker:{self.config.agent_version}",
            reason="lease taken",
        )

        notice = ToolCall(
            "c-notice",
            "send_message",
            {
                "order_id": ORDER,
                "body": "Your return is under review by a specialist.",
            },
        )
        envelope = self.dispatch(envelope, notice)

        refund = ToolCall(
            "c-refund",
            "issue_refund",
            {
                "order_id": ORDER,
                "amount_cents": AMOUNT_CENTS,
                "reason": "fraud_review",
            },
        )
        return self.step(envelope, refund)
