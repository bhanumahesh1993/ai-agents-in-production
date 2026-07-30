"""The policy middleware: one function on the path of every tool call.

Note what it raises and what it returns, because the asymmetry is the
design. Budget exhaustion and policy denial **end the run**: they are
raised, and callers do not catch them. An approval requirement is a **state
the run lives in**, so it comes back as an outcome the loop turns into a
checkpoint, a ``waiting_approval`` status, and an ``approval.requested``
event.

The boundary fails closed, and the list of what closes it is specific:
unknown tool or tool version, ambiguous schema, expired delegation, missing
owner, mismatched tenant, stale approval, unexpected data classification,
and a policy service that is unavailable for a high-risk write. The last
one is the one that gets argued about, and the honest position is that
reads may degrade and high-risk writes may not.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from budget import BudgetGuard
from classes import POLICY_VERSION, class_for
from fingerprint import ToolVersions, fingerprint
from inbox import ResumeState, TaskInbox
from northstar_contracts import (
    RunState,
    ToolCall,
    ToolResult,
    ToolSpec,
    World,
    content_hash,
)
from northstar_policy import Decision, PolicyEngine, PolicyVerdict, Principal
from northstar_runtime import PolicyDenied, ToolRegistry
from outcomes import GuardOutcome
from payload import approval_payload

__all__ = ["PolicyDenied", "PolicyUnavailable", "Guard"]


class PolicyUnavailable(RuntimeError):
    """The decision point could not be reached for a high-risk write.

    A policy service that fails open is a policy service an attacker can
    remove by taking it offline.
    """


class Guard:
    """Authenticate, authorise, budget, fingerprint, and record.

    Args:
        policy: The decision point. Never consulted about anything the
            model wrote; it reads the principal, the scopes, the amount,
            and the spend so far.
        principal: The user, the agent, and the operator, kept separate so
            a rule can say "the agent may issue refunds only for orders
            belonging to the user this run is acting for".
        inbox: Where a required approval goes and where a decision is
            looked up.
        budget: The five hard caps.
        tools: The registry, for tool versions and write flags.
        tool_versions: Declared versions, part of every fingerprint.
        world: The target system, dry-run for the payload's preview.
        authorised_roles: Roles that currently hold approval authority.
        policy_available: Returns ``False`` to simulate the decision point
            being unreachable, so the fail-closed branch has a test.
        clock: Injectable time source.
    """

    def __init__(
        self,
        policy: PolicyEngine,
        principal: Principal,
        inbox: TaskInbox,
        budget: BudgetGuard,
        tools: ToolRegistry,
        tool_versions: ToolVersions,
        world: World,
        *,
        authorised_roles: frozenset[str] = frozenset({"rota:fraud-review"}),
        policy_available: Callable[[], bool] | None = None,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self.policy = policy
        self.principal = principal
        self.approvals = inbox
        self.budget = budget
        self.tools = tools
        self.tool_versions = tool_versions
        self.world = world
        self.authorised_roles = authorised_roles
        self.policy_available = policy_available or (lambda: True)
        self._clock = clock or (lambda: 0.0)
        #: Every outcome, in order. The audit trail of the boundary itself.
        self.decisions: list[dict[str, Any]] = []
        #: Tool results this run has actually seen. The payload's evidence
        #: comes from here rather than from the model's account of itself.
        self.observations: list[dict[str, Any]] = []
        self._cache: dict[tuple[str, int, str, int], GuardOutcome] = {}

    # -------------------------------------- the loop's PolicyEngine interface

    def evaluate(
        self,
        principal: Principal,
        call: ToolCall,
        ctx: dict[str, Any],
    ) -> Decision:
        """Adapt the middleware to the loop's decision-point protocol.

        The agent loop asks a ``PolicyEngine`` for a :class:`Decision` and
        turns ``REQUIRE_APPROVAL`` into a checkpoint and a suspension. That
        is exactly the asymmetry :meth:`guard` implements, so the boundary
        the chapter prints is the boundary the real loop runs through
        rather than a second implementation beside it.
        """
        return self.evaluate_verbose(principal, call, ctx).decision

    def evaluate_verbose(
        self,
        principal: Principal,
        call: ToolCall,
        ctx: dict[str, Any],
    ) -> PolicyVerdict:
        """The decision with its reason, for the approver's payload."""
        state = RunState(
            run_id=str(ctx.get("run_id", "")),
            step=int(ctx.get("step", 0)),
            budget_spent_cents=int(ctx.get("budget_spent_cents", 0)),
        )
        try:
            outcome = self._guard_once(call, state)
        except PolicyDenied as exc:
            return PolicyVerdict(Decision.DENY, "boundary.denied", exc.reason)
        if outcome.ok:
            return PolicyVerdict(
                Decision.ALLOW, "boundary.allowed", outcome.reason
            )
        return PolicyVerdict(
            Decision.REQUIRE_APPROVAL, "boundary.awaits_human", outcome.reason
        )

    def _guard_once(self, call: ToolCall, state: RunState) -> GuardOutcome:
        """:meth:`guard`, memoised per run, step, and exact call.

        The loop asks for a decision and then asks again for its reason.
        Without this, a single proposed call would open two approval
        requests and consume the decision twice, which is the sort of
        detail that makes a control look flaky rather than wrong.

        The count of recorded *decisions* is part of the key, so a human
        saying yes invalidates the memo while merely opening a request
        does not. A memo that outlived an approval would be a control that
        ignores the human, which is a more interesting bug than the one
        the memo is here to prevent.
        """
        decided = sum(
            1 for e in self.approvals.audit if e.get("event") == "decided"
        )
        key = (
            state.run_id,
            state.step,
            content_hash(call.to_dict()),
            decided,
        )
        cached = self._cache.get(key)
        if cached is not None:
            return cached
        outcome = self.guard(call, state)
        self._cache[key] = outcome
        return outcome

    # ---------------------------------------------------------- the middleware

    def guard(self, call: ToolCall, state: RunState) -> GuardOutcome:
        """Decide what happens to one proposed call.

        Raises:
            BudgetExceeded: A hard cap broke. Ends the run.
            PolicyDenied: The decision point refused. Ends the run.
            PolicyUnavailable: The decision point is unreachable and this
                is a high-risk write. Ends the run, on purpose.
        """
        self.budget.check(state)
        self._require_known_tool(call)
        self._require_policy_for_writes(call)
        self.budget.reserve(call, self.tools.spec_for(call.name))

        ctx = self.context_for(call, state)
        decision = self.policy.evaluate(self.principal, call, ctx)
        reason = self._reason_for(call, ctx)

        if decision is Decision.DENY:
            self._record(call, state, "deny", reason)
            raise PolicyDenied(call, reason)
        if decision is Decision.ALLOW:
            outcome = GuardOutcome.proceed("", reason)
            self._record(call, state, "allow", reason)
            return outcome

        fp = fingerprint(
            call,
            self.principal,
            state.run_id,
            self.tool_versions.version(call.name),
        )
        record = self.approvals.find(fp)
        if record is None:
            self._request(call, state, fp, reason)
            self._record(call, state, "wait", reason, fp)
            return GuardOutcome.wait(fp, reason)

        outcome = record.check(fp, state.step, self._clock(), self.current())
        if outcome.ok and not self.approvals.consume(fp, state.step):
            outcome = GuardOutcome.wait(
                fp, "decision already consumed at another step", outcome.checks
            )
        self._record(call, state, outcome.action, outcome.reason, fp)
        return outcome

    # ------------------------------------------------------------ the checks

    def _require_known_tool(self, call: ToolCall) -> None:
        """Fail closed on an unknown tool or an unknown tool version."""
        if self.tools.spec_for(call.name) is None:
            raise PolicyDenied(call, "unknown tool")
        try:
            self.tool_versions.version(call.name)
        except KeyError as exc:
            raise PolicyDenied(call, "unknown tool version") from exc

    def _require_policy_for_writes(self, call: ToolCall) -> None:
        """Reads may degrade when the decision point is down. Writes may not.

        Raises:
            PolicyUnavailable: On a write, when the policy service is
                unreachable.
        """
        if self.policy_available():
            return
        spec = self.tools.spec_for(call.name)
        if spec is not None and spec.writes:
            raise PolicyUnavailable(
                f"policy service unavailable; {call.name} is a write, so "
                f"the boundary fails closed"
            )

    def current(self) -> ResumeState:
        """The facts a resume checks a stored decision against."""
        return ResumeState(
            tool_version=self.tool_versions.version("issue_refund"),
            policy_version=POLICY_VERSION,
            world_version=self.world_version(),
            authorised=self.authorised_roles,
            still_routes_to_human=True,
        )

    def world_version(self, order_id: str = "NR-2026-0042110") -> str:
        """A row version for the order under decision.

        Without this, two runs can each obtain a valid approval for a
        refund on the same order and both execute correctly.
        """
        rows = self.world.refunds_for(order_id)
        return (
            f"{order_id}:{len(rows)}:"
            f"{self.world.total_refunded_cents(order_id)}"
        )

    def context_for(self, call: ToolCall, state: RunState) -> dict[str, Any]:
        """What the decision point reads. All of it held by the runtime."""
        spec = self.tools.spec_for(call.name)
        return {
            "run_id": state.run_id,
            "step": state.step,
            "budget_spent_cents": state.budget_spent_cents,
            "writes": bool(spec and spec.writes),
            "action_class": class_for(call).name,
        }

    # --------------------------------------------------------------- helpers

    def _reason_for(self, call: ToolCall, ctx: dict[str, Any]) -> str:
        """The justification shown to an approver, from the rule itself."""
        verbose = getattr(self.policy, "evaluate_verbose", None)
        if verbose is None:
            return ""
        verdict = verbose(self.principal, call, ctx)
        return verdict.reason or verdict.rule

    def note(
        self,
        call: ToolCall,
        result: ToolResult,
        spec: ToolSpec | None,
    ) -> None:
        """Record a landed tool result.

        Two jobs, both of which have to happen *after* the effect rather
        than before it. The result becomes evidence in the next approval
        payload, and a successful write is counted against the run's write
        and distinct-resource caps.
        """
        if not result.ok:
            return
        self.observations.append(
            {"tool": call.name, "ok": True, "content": result.content}
        )
        self.budget.observe(call, spec)

    def _request(
        self,
        call: ToolCall,
        state: RunState,
        fp: str,
        reason: str,
    ) -> None:
        """Open a request and attach the payload the approver will read."""
        request = self.approvals.request(
            state.run_id, state.step, call, reason=reason
        )
        payload = approval_payload(
            call,
            fingerprint=fp,
            principal=self.principal,
            tool_version=self.tool_versions.version(call.name),
            world=self.world,
            observations=self.observations,
            reason=reason,
            expires_at=request.expires_at,
        )
        self.approvals.attach(request.id, payload, self.current())

    def _record(
        self,
        call: ToolCall,
        state: RunState,
        action: str,
        reason: str,
        fp: str = "",
    ) -> None:
        """Append one boundary decision to the guard's own audit trail."""
        self.decisions.append(
            {
                "run_id": state.run_id,
                "step": state.step,
                "tool": call.name,
                "arguments": dict(call.arguments),
                "action": action,
                "reason": reason,
                "fingerprint": fp,
                "action_class": class_for(call).name,
                "principal": self.principal.to_dict(),
                "budget": self.budget.snapshot(),
            }
        )
