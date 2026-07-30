"""Topology two: a swarm, where control transfers instead of returning.

The triage agent hands the ticket to the fraud agent and stops. No round
trip through a coordinator, so the turn count is lower — that is a real
advantage rather than a rounding error. Each of those turns costs more,
because the accumulated transcript travels with the transfer while the
supervisor's workers receive a scoped brief and return a bounded summary.
Which of the two effects wins on the *total* depends on how long the
conversation is by the time control moves, and `compare.py` prints both so
you can see which one you are buying.

The configuration flag is ``carry_contract``. With it, the receiving agent
derives its idempotency key from the origin run and step in the
:class:`~handoff.Handoff`. Without it, the receiver has a fresh run id, so a
retried step presents a new key and pays a second time. Same agents, same
tools, same fault, and the difference is 12,000 cents.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

import topology
from handoff import Handoff, refund_key, refund_key_local
from northstar_contracts import (
    Message,
    RunState,
    ToolCall,
    ToolSpec,
    World,
)
from northstar_policy import ApprovalStore
from northstar_runtime import AgentLoop, FakeModel, ToolRegistry
from topology import (
    GOAL,
    ORDER_ID,
    ORIGIN_RUN_ID,
    ORIGIN_STEP_ID,
    REASON,
    REFUND_CENTS,
    KeyedRegistry,
    Trace,
)

__all__ = [
    "FRAUD_RUN_ID",
    "TRANSFER",
    "TRIAGE_RUN_ID",
    "SwarmRun",
    "run_swarm",
]

TRIAGE_RUN_ID = ORIGIN_RUN_ID
FRAUD_RUN_ID = "run_01H8XQ6FRAUDREVIEW"

TRANSFER = ToolSpec(
    name="transfer_to_fraud",
    description=(
        "Transfer responsibility for this ticket to the fraud-review agent. "
        "Control does not come back: state what must be returned, to whom, "
        "and what happens if it is not."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "goal": {"type": "string"},
            "turns_left": {"type": "integer"},
        },
        "required": ["goal"],
    },
    output_schema={"type": "object"},
    writes=False,
    idempotent=True,
    max_result_tokens=600,
)

#: What the receiver is allowed to do. A subset of the sender's set, and the
#: threshold travels with it: enumerate the constraints at every hop or the
#: 5,000-cent gate evaporates one transfer away from the agent written to
#: respect it.
FRAUD_TOOLS = ("get_order", "get_policy", "issue_refund", "send_message")


@dataclass
class SwarmRun:
    """Both halves of one swarm run, and the contract between them."""

    triage: RunState
    fraud: RunState | None
    handoff: Handoff | None
    trace: Trace
    registry: KeyedRegistry | None


def _triage_registry(world: World, sink: dict[str, Any]) -> ToolRegistry:
    """Reads plus the transfer. The triage agent cannot write."""
    registry = ToolRegistry()
    for spec, fn in world.tools():
        if spec.name in ("get_order", "get_policy", "search_orders"):
            registry.register(spec, fn)
    registry.register(TRANSFER, _make_transfer(sink))
    return registry


def _make_transfer(sink: dict[str, Any]) -> Any:
    """Build the transfer tool. It constructs the contract and files it."""

    def transfer_to_fraud(goal: str, turns_left: int = 6) -> dict[str, Any]:
        contract = Handoff(
            origin_run_id=ORIGIN_RUN_ID,
            origin_step_id=ORIGIN_STEP_ID,
            goal=goal,
            allowed_tools=FRAUD_TOOLS,
            prohibited_tools=("escalate_to_specialist",),
            approval_threshold_cents=5000,
            budget_cents_left=200,
            turns_left=turns_left,
            return_to="support-triage",
            deadline_ts=time.time() + 900.0,
            evidence_refs=(f"artifact://orders/{ORDER_ID}",),
            chain=("support-triage@2.0.0",),
        )
        sink["handoff"] = contract
        return {
            "transferred_to": contract.to_agent,
            "goal": contract.goal,
            "return_to": contract.return_to,
            "on_timeout": contract.on_timeout,
        }

    return transfer_to_fraud


TRIAGE_SCRIPT: dict[str, list[Any]] = {
    "Ticket 9104": [
        ToolCall("t1", "get_order", {"order_id": ORDER_ID}),
        ToolCall(
            "t2",
            "transfer_to_fraud",
            {
                "goal": (
                    "Assess and settle the damaged Field Speaker on order "
                    "NR-2026-0042110; refund one unit if defensible."
                ),
                "turns_left": 6,
            },
        ),
        "Transferred to fraud-review; they own the outcome from here.",
    ]
}


def _fraud_registry(
    world: World,
    contract: Handoff,
    carry_contract: bool,
) -> KeyedRegistry:
    """The receiving agent's tools, and the one line that decides the ledger."""
    if carry_contract:
        # Derive from the ORIGIN run, carried across the hop. Constant for
        # every attempt at this intent, wherever it is executed.
        key_for = _carried(contract)
    else:
        # The receiver's own run and step. A fresh identity per attempt.
        key_for = _local

    registry = KeyedRegistry(key_for)
    for spec, fn in world.tools():
        if spec.name in contract.allowed_tools:
            contract.require(spec.name)
            registry.register(spec, fn)
    return registry


def _carried(contract: Handoff) -> Any:
    """Bind :func:`refund_key` to this transfer's contract."""
    key = refund_key(contract)
    return lambda _run, _step: key


def _local(run_id: str, step: int) -> str:
    """Bind :func:`refund_key_local` to the receiver's current position."""
    return refund_key_local(RunState(run_id=run_id, step=step))


def _fraud_decide(messages: list[Message]) -> ToolCall | str:
    """The receiving agent's trajectory, as a function of its observations."""
    state = RunState(run_id=FRAUD_RUN_ID, messages=list(messages))
    seen = topology.observations(state)

    if not topology.succeeded(state, "get_policy"):
        return ToolCall(
            "g1", "get_policy", {"reason": REASON, "sku": "NR-SPEAKER-09"}
        )
    if not topology.succeeded(state, "issue_refund"):
        attempt = sum(1 for o in seen if o.get("tool") == "issue_refund") + 1
        return ToolCall(
            f"g-refund-{attempt}",
            "issue_refund",
            {
                "order_id": ORDER_ID,
                "amount_cents": REFUND_CENTS,
                "reason": REASON,
            },
        )
    if not topology.succeeded(state, "send_message"):
        return ToolCall(
            "g-msg",
            "send_message",
            {
                "order_id": ORDER_ID,
                "body": (
                    "We have refunded US$120.00 for the damaged Field "
                    "Speaker. The second unit is unaffected."
                ),
            },
        )
    return "Refunded one Field Speaker at 12000 cents and confirmed it."


def run_swarm(world: World, carry_contract: bool = True) -> SwarmRun:
    """Run the swarm topology, with or without the contract's provenance."""
    trace = Trace()
    sink: dict[str, Any] = {}

    triage = AgentLoop(
        model=FakeModel(TRIAGE_SCRIPT),
        tools=_triage_registry(world, sink),
        policy=topology.policy(),
        telemetry=trace.tap("support-triage"),
        max_turns=6,
        budget_cents=100,
        principal=topology.principal(),
    )
    triage_state = triage.run(GOAL, run_id=TRIAGE_RUN_ID)

    contract = sink.get("handoff")
    if contract is None:
        return SwarmRun(triage_state, None, None, trace, None)

    registry = _fraud_registry(world, contract, carry_contract)
    approvals = ApprovalStore()
    fraud = AgentLoop(
        model=FakeModel(default=[_fraud_decide] * 8),
        tools=registry,
        policy=topology.policy(),
        approvals=approvals,
        telemetry=trace.tap("fraud-review"),
        max_turns=contract.turns_left,
        budget_cents=contract.budget_cents_left,
        principal=topology.principal(),
    )
    state = fraud.start(contract.goal, run_id=FRAUD_RUN_ID)
    # The accumulated transcript travels with the transfer. It arrives as
    # input rather than as history the receiver produced, which is both
    # honest about who spent the turns and the reason a swarm costs more
    # tokens than the supervisor it beats on turn count.
    state = state.with_messages(
        Message(role="user", content=topology.transcript_of(triage_state))
    )
    state = fraud.resume(state)
    state = topology.decide_and_resume(fraud, approvals, state)
    return SwarmRun(triage_state, state, contract, trace, registry)
