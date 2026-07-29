"""The agent loop.

Reason, act, observe, repeat, until the model stops asking for tools or
something outside the model stops the run. That is the whole idea, and it
fits on a page. Everything else in this file is the difference between a
demo and a system you can put a customer's money behind:

* a **budget** the model does not get a vote on;
* a **policy** decision on every call, before any side effect;
* a **checkpoint** after every step, so a restart is survivable;
* an **event log** on every decision, so an incident is explicable;
* and a rule that **tool failures are observations, not exceptions**.

The loop raises in exactly four situations: budget exhaustion, turn
exhaustion, policy denial, and cancellation. Those are the cases where
continuing would be worse than stopping. A tool that times out is not one
of them.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Iterable, Sequence
from typing import Any, Protocol, runtime_checkable

from northstar_contracts import (
    EventLog,
    Message,
    Money,
    RunState,
    ToolCall,
    ToolResult,
    ToolSpec,
    estimate_tokens,
)
from northstar_policy import (
    ApprovalStore,
    BudgetExceeded,
    BudgetGuard,
    Decision,
    PolicyEngine,
    Principal,
)

from .providers import ModelProvider, ModelResponse
from .registry import ToolFn, ToolRegistry

__all__ = [
    "DEFAULT_SYSTEM_PROMPT",
    "AgentLoop",
    "AgentLoopError",
    "PolicyDenied",
    "RunCancelled",
    "TelemetrySink",
    "default_cost_cents",
]

DEFAULT_SYSTEM_PROMPT = """\
You are the Northstar Returns support agent.

Work from the tools, not from memory. Read the order and the refund policy
before you quote a figure or promise an outcome. Amounts are always integer
cents.

Before any write, satisfy yourself that it is the smallest action that
resolves the customer's problem. Writes that move money or reach the
customer are irreversible; a read costs a fraction of a cent.

If something looks like fraud, escalate rather than deciding. If a call
fails, read the error before retrying: some failures mean the work did not
happen, and some mean you cannot tell.

Finish with a short plain-English answer for the customer."""


@runtime_checkable
class TelemetrySink(Protocol):
    """Anything that wants a copy of every event as it happens."""

    def emit(self, record: dict[str, Any]) -> None:
        """Handle one event-log record."""
        ...


class AgentLoopError(RuntimeError):
    """Base class for the failures the loop raises on purpose."""


class PolicyDenied(AgentLoopError):
    """The policy decision point refused a call.

    Not recoverable inside the run. The model asked to do something it is
    not allowed to do; letting it observe the denial and try a variation is
    how a determined prompt injection eventually finds the wording that
    works.
    """

    def __init__(self, call: ToolCall, reason: str = "") -> None:
        self.call = call
        self.reason = reason
        detail = f": {reason}" if reason else ""
        super().__init__(f"policy denied {call.name}{detail}")


class RunCancelled(AgentLoopError):
    """The run was cancelled, by an operator or a kill switch."""

    def __init__(self, run_id: str, reason: str = "") -> None:
        self.run_id = run_id
        self.reason = reason
        detail = f": {reason}" if reason else ""
        super().__init__(f"run {run_id} cancelled{detail}")


def default_cost_cents(response: ModelResponse) -> Money:
    """A placeholder cost model, in integer cents.

    Deliberately crude and deliberately not a price claim: output tokens
    cost more than input tokens, and that is the only property the examples
    need. For real per-model pricing use
    :class:`northstar_telemetry.CostLedger`, which takes a price table you
    supply and date-stamp yourself.

    Returns:
        Cost in whole cents, rounded up, so a cheap turn still costs
        something and a budget of 200 cents is a budget of 200 turns at
        worst rather than infinity.
    """
    units = response.input_tokens + 5 * response.output_tokens
    return max(1, -(-units // 2000))


class AgentLoop:
    """A provider-agnostic reason-act-observe loop.

    Args:
        model: Anything satisfying :class:`~northstar_runtime.providers.ModelProvider`.
        tools: A :class:`~northstar_runtime.registry.ToolRegistry`, or
            spec/implementation pairs to build one from.
        checkpointer: Saves state after every step. Without one the run
            cannot survive the process.
        policy: Decision point consulted before every tool call.
        telemetry: Sink receiving every event. See
            :func:`northstar_telemetry.instrument`.
        max_turns: Hard turn ceiling. Exhausting it raises.
        budget_cents: Hard money ceiling. Exhausting it raises.
        principal: Who this run acts as. Defaults to an anonymous
            principal with no scopes, which will fail any scope check —
            the right default, because a run with no identity should not
            be able to spend money.
        approvals: Store consulted when the policy asks for a human.
        system_prompt: Overrides :data:`DEFAULT_SYSTEM_PROMPT`.
        cost_fn: Maps a model response to cents.
        tool_retries: Extra attempts for a failed call, applied only when
            :meth:`~northstar_runtime.registry.ToolRegistry.is_retry_safe`
            says a repeat cannot double an effect.
        max_wall_seconds: Wall-clock ceiling for the run.
        step_hook: Called with the state after each committed step. The
            durable runner uses it to simulate a crash at an exact point.

    Example:
        >>> from northstar_contracts import World
        >>> world = World()
        >>> loop = AgentLoop(
        ...     model=FakeModel(default=["All set."]),
        ...     tools=world.tools(),
        ... )
        >>> loop.run("check order NR-2026-0041827").status
        'succeeded'
    """

    def __init__(
        self,
        model: ModelProvider,
        tools: ToolRegistry | Iterable[tuple[ToolSpec, ToolFn]],
        checkpointer: Any | None = None,
        policy: PolicyEngine | None = None,
        telemetry: TelemetrySink | None = None,
        max_turns: int = 12,
        budget_cents: Money = 200,
        *,
        principal: Principal | None = None,
        approvals: ApprovalStore | None = None,
        system_prompt: str | None = None,
        cost_fn: Callable[[ModelResponse], Money] | None = None,
        tool_retries: int = 1,
        max_wall_seconds: float | None = None,
        step_hook: Callable[[RunState], None] | None = None,
    ) -> None:
        self.model = model
        self.tools = (
            tools
            if isinstance(tools, ToolRegistry)
            else ToolRegistry().register_all(tools)
        )
        self.checkpointer = checkpointer
        self.policy = policy
        self.telemetry = telemetry
        self.approvals = approvals
        self.principal = principal or Principal()
        self.system_prompt = system_prompt or DEFAULT_SYSTEM_PROMPT
        self.cost_fn = cost_fn or default_cost_cents
        self.tool_retries = tool_retries
        self.step_hook = step_hook
        self.budget = BudgetGuard(
            max_cents=budget_cents,
            max_turns=max_turns,
            max_wall_seconds=max_wall_seconds,
        )
        self.events = EventLog(sink=self._forward)
        self.current_run_id: str | None = None
        self._cancel_reason: str | None = None

    # ---------------------------------------------------------------- public

    def run(self, goal: str, run_id: str | None = None) -> RunState:
        """Run to completion, a human wait, or a hard stop.

        Args:
            goal: What the agent is being asked to do.
            run_id: Supply one for a reproducible run; otherwise generated.

        Returns:
            The final :class:`~northstar_contracts.models.RunState`. A
            status of ``waiting_approval`` means the run is suspended, not
            finished: decide the approval and call :meth:`resume`.

        Raises:
            BudgetExceeded: On money, turn, or wall-clock exhaustion.
            PolicyDenied: When policy refuses a call.
            RunCancelled: When the run was cancelled.
        """
        return self.resume(self.start(goal, run_id=run_id))

    def start(self, goal: str, run_id: str | None = None) -> RunState:
        """Create the initial state and emit ``run.started``."""
        run_id = run_id or f"run-{uuid.uuid4().hex[:12]}"
        self.current_run_id = run_id
        self._cancel_reason = None
        self.budget.run_id = run_id
        self.budget.spent_cents = 0
        self.budget.turns = 0
        self.budget.start()

        state = RunState(
            run_id=run_id,
            step=0,
            messages=[
                Message(role="system", content=self.system_prompt),
                Message(role="user", content=goal),
            ],
            status="running",
        )
        self.events.emit(
            run_id,
            0,
            "run.started",
            {
                "goal": goal,
                "principal": self.principal.to_dict(),
                "tools": self.tools.names(),
                "max_turns": self.budget.max_turns,
                "budget_cents": self.budget.max_cents,
            },
        )
        self._checkpoint(state)
        return state

    def resume(self, state: RunState) -> RunState:
        """Drive a run forward until it stops.

        Safe to call on a freshly started run, on a state loaded from a
        checkpoint in another process, and on a run that was suspended for
        an approval. Restores the budget counters from the state, so a
        resumed run cannot quietly get a second full budget — which is the
        bug you get for free if you rebuild the guard and forget.
        """
        self.current_run_id = state.run_id
        self.budget.run_id = state.run_id
        self.budget.spent_cents = state.budget_spent_cents
        self.budget.turns = state.step
        self.budget.start()

        state = self._dispatch_pending(state)
        while state.status == "running":
            state = self.step(state)

        if state.status != "waiting_approval":
            self.events.emit(
                state.run_id,
                state.step,
                "run.finished",
                {
                    "status": state.status,
                    "budget_spent_cents": state.budget_spent_cents,
                },
            )
        return state

    def cancel(self, reason: str = "operator cancelled") -> None:
        """Ask the loop to stop at the next step boundary.

        Cancellation lands *between* steps, never inside one. A cancel that
        interrupts a tool mid-write leaves you with the same ambiguity as a
        timeout, and the whole point of a kill switch is to remove
        ambiguity.
        """
        self._cancel_reason = reason

    def step(self, state: RunState) -> RunState:
        """Take exactly one turn: one model call and its tool calls."""
        self._raise_if_cancelled(state)
        step_index = state.step
        self.budget.tick()

        response = self.model.complete(
            list(state.messages), self.tools.specs()
        )
        cents = int(self.cost_fn(response))
        self.events.emit(
            state.run_id,
            step_index,
            "model.called",
            {
                "model": response.model,
                "input_tokens": response.input_tokens,
                "output_tokens": response.output_tokens,
                "stop_reason": response.stop_reason,
                "cost_cents": cents,
                "tool_calls": [c.name for c in response.tool_calls],
            },
        )

        state = state.with_messages(response.as_message())
        state = state.advance(spent_cents=cents)
        self._charge(state, cents)

        if not response.tool_calls:
            state = state.with_status("succeeded")
            self._checkpoint(state)
            self._after_step(state)
            return state

        # Authorise every call in the batch before dispatching any of
        # them. A batch that is half executed and half blocked leaves the
        # world in a state no one designed.
        if not self._authorize_all(state, step_index, response.tool_calls):
            state = state.with_status("waiting_approval")
            self._checkpoint(state)
            self._after_step(state)
            return state

        observations = self._dispatch_all(
            state, step_index, response.tool_calls
        )
        state = state.with_messages(*observations)
        self._checkpoint(state)
        self._after_step(state)
        return state

    # --------------------------------------------------------------- helpers

    def _dispatch_pending(self, state: RunState) -> RunState:
        """Run calls that an approval gate left undispatched.

        When the loop suspends, the model's decision is already recorded as
        an assistant message. Resuming re-authorises those exact calls and
        runs them; it does not ask the model again. The model has already
        decided, and re-deciding on a resumed run means the thing the human
        approved is not necessarily the thing that runs.
        """
        if state.status != "waiting_approval":
            return state
        pending = self._pending_calls(state)
        if not pending:
            return state.with_status("running")

        step_index = max(0, state.step - 1)
        if not self._authorize_all(state, step_index, pending):
            return state

        observations = self._dispatch_all(state, step_index, pending)
        state = state.with_messages(*observations).with_status("running")
        self._checkpoint(state)
        return state

    @staticmethod
    def _pending_calls(state: RunState) -> list[ToolCall]:
        """Tool calls the model asked for that have no observation yet."""
        if not state.messages:
            return []
        last = state.messages[-1]
        return last.tool_calls if last.role == "assistant" else []

    def _authorize_all(
        self,
        state: RunState,
        step_index: int,
        calls: Sequence[ToolCall],
    ) -> bool:
        """Authorise a batch. ``False`` means a human has to decide.

        Every call is evaluated, not just up to the first blocker, so an
        approver sees the whole batch at once rather than being asked the
        same question three times in a row.

        Raises:
            PolicyDenied: On the first denied call.
        """
        verdicts = [
            self._authorize(state, step_index, call) for call in calls
        ]
        return all(verdicts)

    def _authorize(
        self,
        state: RunState,
        step_index: int,
        call: ToolCall,
    ) -> bool:
        """Authorise one call. ``False`` means it needs an approval."""
        if self.policy is None:
            return True

        ctx: dict[str, Any] = {
            "run_id": state.run_id,
            "step": step_index,
            "budget_spent_cents": state.budget_spent_cents,
            "writes": bool(
                (spec := self.tools.spec_for(call.name)) and spec.writes
            ),
        }
        decision = self.policy.evaluate(self.principal, call, ctx)
        reason = self._reason_for(state, call, ctx)

        if decision is Decision.DENY:
            raise PolicyDenied(call, reason)
        if decision is not Decision.REQUIRE_APPROVAL:
            return True

        if self.approvals is None:
            self.events.emit(
                state.run_id,
                step_index,
                "approval.requested",
                {
                    "tool": call.name,
                    "arguments": call.arguments,
                    "reason": reason,
                    "note": "no approval store configured; run suspended",
                },
            )
            return False

        if self.approvals.is_approved(call, state.run_id):
            self.events.emit(
                state.run_id,
                step_index,
                "approval.decided",
                {
                    "tool": call.name,
                    "arguments": call.arguments,
                    "approved": True,
                },
            )
            return True

        request = self.approvals.request(
            state.run_id,
            step_index,
            call,
            reason=reason,
            principal=self.principal,
        )
        self.events.emit(
            state.run_id,
            step_index,
            "approval.requested",
            {
                "request_id": request.id,
                "fingerprint": request.fingerprint,
                "tool": call.name,
                "arguments": call.arguments,
                "reason": reason,
            },
        )
        return False

    def _reason_for(
        self,
        state: RunState,
        call: ToolCall,
        ctx: dict[str, Any],
    ) -> str:
        """The human-readable justification shown to an approver."""
        verbose = getattr(self.policy, "evaluate_verbose", None)
        if verbose is None:
            return ""
        verdict = verbose(self.principal, call, ctx)
        return verdict.reason or verdict.rule

    def _dispatch_all(
        self,
        state: RunState,
        step_index: int,
        calls: Sequence[ToolCall],
    ) -> list[Message]:
        """Run a batch of calls and return their observation messages."""
        observations: list[Message] = []
        for call in calls:
            result = self._dispatch_with_retry(state, step_index, call)
            observations.append(self._observation(call, result))
        return observations

    def _dispatch_with_retry(
        self,
        state: RunState,
        step_index: int,
        call: ToolCall,
    ) -> ToolResult:
        """Dispatch one call, retrying only when a retry cannot double it.

        This is the harness-level repair for the Chapter 1 incident. A
        timeout on a keyed refund is retried here, with the *same* key, so
        the store recognises the second attempt as the first one and the
        model never sees a failure to react badly to.

        A timeout on an unkeyed refund is not retried, because the runtime
        genuinely cannot tell whether the money moved. It hands the model
        an honest error instead. The model may then retry blindly — and
        that is the incident, faithfully reproduced, because the fix
        belongs in the tool contract and not in the prompt.
        """
        attempts = 1
        if self.tool_retries and self.tools.is_retry_safe(call):
            attempts += self.tool_retries

        result = ToolResult.failure(call.id, "not dispatched")
        for attempt in range(1, attempts + 1):
            self.events.emit(
                state.run_id,
                step_index,
                "tool.called",
                {
                    "call_id": call.id,
                    "tool": call.name,
                    "arguments": call.arguments,
                    "attempt": attempt,
                },
            )
            result = self.tools.dispatch(
                call, run_id=state.run_id, step=step_index
            )
            self.events.emit(
                state.run_id,
                step_index,
                "tool.result",
                {
                    "call_id": call.id,
                    "tool": call.name,
                    "ok": result.ok,
                    "truncated": result.truncated,
                    "retryable": result.retryable,
                    "attempt": attempt,
                    "result_tokens": estimate_tokens(result.content),
                    "error": result.error,
                },
            )
            if result.ok or not result.retryable:
                break
        return result

    @staticmethod
    def _observation(call: ToolCall, result: ToolResult) -> Message:
        """Render a tool result as the message the model will read."""
        return Message(
            role="tool",
            content={
                "call_id": result.call_id,
                "tool": call.name,
                "ok": result.ok,
                "truncated": result.truncated,
                "content": result.content,
            },
        )

    def _charge(self, state: RunState, cents: Money) -> None:
        """Charge the budget, failing the run cleanly if it breaks."""
        try:
            self.budget.charge(cents)
        except BudgetExceeded:
            failed = state.with_status("failed")
            self._checkpoint(failed)
            self.events.emit(
                state.run_id,
                state.step,
                "run.finished",
                {
                    "status": "failed",
                    "reason": "budget exceeded",
                    "budget_spent_cents": failed.budget_spent_cents,
                },
            )
            raise

    def _checkpoint(self, state: RunState) -> None:
        """Persist state and record that we did."""
        if self.checkpointer is None:
            return
        self.checkpointer.save(state)
        self.events.emit(
            state.run_id,
            state.step,
            "checkpoint.written",
            {"status": state.status, "messages": len(state.messages)},
        )

    def _after_step(self, state: RunState) -> None:
        """Run the step hook, after the step is durably recorded."""
        if self.step_hook is not None:
            self.step_hook(state)

    def _raise_if_cancelled(self, state: RunState) -> None:
        """Stop the run if someone pulled the switch."""
        if self._cancel_reason is None:
            return
        cancelled = state.with_status("cancelled")
        self._checkpoint(cancelled)
        self.events.emit(
            state.run_id,
            state.step,
            "run.finished",
            {"status": "cancelled", "reason": self._cancel_reason},
        )
        raise RunCancelled(state.run_id, self._cancel_reason)

    def _forward(self, record: dict[str, Any]) -> None:
        """Forward one event to the telemetry sink, if one is attached.

        Read late on purpose: :func:`northstar_telemetry.instrument` can
        attach a sink to a loop that has already been constructed.
        """
        if self.telemetry is not None:
            self.telemetry.emit(record)
