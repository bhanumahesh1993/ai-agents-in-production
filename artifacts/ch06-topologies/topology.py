"""One task, three topologies, and the instrumentation that prices them.

The task is a refund request on order ``NR-2026-0042110``, the US$240.00
order flagged for fraud review. It needs a policy lookup, a fraud
assessment, and a refund that exceeds the 5,000-cent threshold and therefore
requires approval.

The refund is 12,000 cents — one of the two Field Speakers — rather than the
whole order. That is load-bearing. The third configuration has to be able to
leave *two* refund rows in the ledger, and on an order whose total equalled
the claim the world's own over-refund guard would reject the second row. The
guard rather than the dropped provenance would then be what stopped the
duplicate, and the chapter would be measuring the wrong thing.

Turn counts are exact because the model is scripted. Token figures come from
each component's own ``model.called`` events, fed into one shared
:class:`~northstar_telemetry.CostLedger`, so a worker's tokens are counted
where the worker spent them.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from northstar_contracts import (
    Money,
    RunState,
    ToolCall,
    World,
    idempotency_key,
)
from northstar_policy import ApprovalStore, Principal, default_northstar_policy
from northstar_runtime import AgentLoop, ToolRegistry
from northstar_telemetry import CostLedger

__all__ = [
    "GOAL",
    "ORDER_ID",
    "ORIGIN_RUN_ID",
    "ORIGIN_STEP_ID",
    "REASON",
    "REFUND_CENTS",
    "KeyedRegistry",
    "Timeout",
    "Trace",
    "build_world",
    "decide_and_resume",
    "observations",
    "principal",
    "policy",
    "succeeded",
    "transcript_of",
]

ORDER_ID = "NR-2026-0042110"
SKU = "NR-SPEAKER-09"
REASON = "damaged"
REFUND_CENTS: Money = 12000
GOAL = (
    "Ticket 9104: one Northstar Field Speaker on order NR-2026-0042110 "
    "arrived damaged. Refund that unit and tell the customer."
)

#: The run and step the refund intent was formed at. Every configuration
#: that keeps the contract derives its key from this pair, whichever
#: component ends up executing the call.
ORIGIN_RUN_ID = "run_01H8XQ6TOPOLOGY"
ORIGIN_STEP_ID = 7


def build_world() -> World:
    """The world with the Chapter 1 fault armed on ``issue_refund``.

    The refund commits and *then* the response is lost, so no caller can
    tell whether the money moved. Every configuration below meets the same
    fault; only the key they present differs.
    """
    world = World()
    world.inject_fault("issue_refund", kind="timeout")
    return world


def principal() -> Principal:
    """The identity every component in this artifact acts under."""
    return Principal.of(
        "CUST-9032", "orders:read", "policy:read", "refunds:write"
    )


def policy() -> Any:
    """Northstar's shipped policy: this order is flagged and over threshold."""
    return default_northstar_policy()


# ------------------------------------------------------------- keyed writes


class KeyedRegistry(ToolRegistry):
    """Stamps ``issue_refund`` with whichever derivation the topology uses.

    The registry rather than the model, because the key belongs to the
    intent and the model has no idea which attempt it is on. Which
    derivation is supplied is the only difference between the second and
    third rows of the comparison table.
    """

    def __init__(
        self,
        key_for: Callable[[str, int], str],
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self.key_for = key_for
        #: Every key presented, in order. The comparison reads this.
        self.keys_presented: list[str] = []

    def dispatch(
        self,
        call: ToolCall,
        run_id: str | None = None,
        step: int | None = None,
    ) -> Any:
        if call.name == "issue_refund":
            key = self.key_for(run_id or "", int(step or 0))
            self.keys_presented.append(key)
            call = ToolCall(
                call.id, call.name, {**call.arguments, "idempotency_key": key}
            )
        return super().dispatch(call, run_id, step)


def anchored_key(run_id: str, step: int) -> Callable[[str, int], str]:
    """A key derived from a fixed origin, whoever executes the call."""
    fixed = idempotency_key(run_id, step)
    return lambda _run, _step: fixed


# --------------------------------------------------------------- the trace


@dataclass(frozen=True)
class Timeout:
    """One observed ``issue_refund`` failure, and who was holding it."""

    component: str
    run_id: str
    step: int
    error: str


@dataclass
class Trace:
    """Turns, tokens, and ownership, collected across every component."""

    ledger: CostLedger = field(default_factory=CostLedger)
    turns_by_component: dict[str, int] = field(default_factory=dict)
    timeouts: list[Timeout] = field(default_factory=list)
    approvals: int = 0

    def tap(self, component: str) -> _Tap:
        """A telemetry sink bound to one component of the topology."""
        return _Tap(component, self)

    @property
    def turns(self) -> int:
        """Model turns taken anywhere in the topology."""
        return sum(self.turns_by_component.values())

    @property
    def tokens(self) -> int:
        """Input plus output tokens, anywhere in the topology."""
        counts = self.ledger.tokens()
        return counts["input_tokens"] + counts["output_tokens"]


class _Tap:
    """Feeds one component's events into the shared trace."""

    def __init__(self, component: str, trace: Trace) -> None:
        self.component = component
        self.trace = trace

    def emit(self, record: dict[str, Any]) -> None:
        """Record a turn, its tokens, or an unowned write."""
        kind = record.get("type")
        payload = record.get("payload") or {}
        if kind == "model.called":
            self.trace.turns_by_component[self.component] = (
                self.trace.turns_by_component.get(self.component, 0) + 1
            )
            self.trace.ledger.record(
                str(payload.get("model", "fake-model-1")),
                int(payload.get("input_tokens", 0)),
                int(payload.get("output_tokens", 0)),
                run_id=str(record.get("run_id", "")),
            )
        elif kind == "approval.requested":
            self.trace.approvals += 1
        elif (
            kind == "tool.result"
            and payload.get("tool") == "issue_refund"
            and not payload.get("ok")
        ):
            self.trace.timeouts.append(
                Timeout(
                    component=self.component,
                    run_id=str(record.get("run_id", "")),
                    step=int(record.get("step", 0)),
                    error=str(payload.get("error", "")),
                )
            )


# ------------------------------------------------------------- run helpers


def decide_and_resume(
    loop: AgentLoop,
    approvals: ApprovalStore,
    state: RunState,
    by: str = "ops@northstar",
) -> RunState:
    """Approve whatever the run suspended for, then drive it on.

    An approval binds one exact call, so a run that suspends twice asks
    twice. It does *not* re-ask for the retry of a call it already cleared:
    the fingerprint excludes the call id, so the second attempt at the same
    intent is recognised as the same question.
    """
    while state.status == "waiting_approval":
        pending = approvals.pending()
        if not pending:
            break
        for request in pending:
            approvals.decide(
                request.id,
                approved=True,
                by=by,
                note="fraud review cleared; refunding one unit",
            )
        state = loop.resume(state)
    return state


def observations(state: RunState) -> list[dict[str, Any]]:
    """Every tool observation this run has seen, in order."""
    return [
        m.content
        for m in state.messages
        if m.role == "tool" and isinstance(m.content, dict)
    ]


def succeeded(state: RunState, tool: str) -> bool:
    """Whether ``tool`` has already returned a result the model believed."""
    return any(
        o.get("tool") == tool and o.get("ok") for o in observations(state)
    )


def transcript_of(state: RunState) -> str:
    """The accumulated conversation, rendered for the next agent to read.

    This is the thing the swarm carries and the supervisor's workers do
    not. Rendering it here rather than passing message objects keeps the
    receiving agent's own turn count honest: a transferred transcript is
    input, not history the receiver produced.
    """
    lines: list[str] = []
    for message in state.messages:
        if message.role == "system":
            continue
        body = message.content
        if isinstance(body, dict):
            body = body.get("content", body)
        lines.append(f"{message.role}: {body}")
    return "Transferred conversation:\n" + "\n".join(str(x) for x in lines)
