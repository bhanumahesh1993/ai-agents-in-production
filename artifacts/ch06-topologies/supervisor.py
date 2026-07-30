"""Topology one: a supervisor with read-only workers reached by delegation.

Every write stays in the supervisor's loop, so exactly one component is
accountable for the outcome of every step. The workers receive a scoped
brief and return a bounded summary, and the return budget is enforced in the
dispatch path — ``max_result_tokens`` on the tool spec — rather than
requested in a prompt asking for brevity.

What it costs is the bottleneck: every subtask makes a round trip through
the coordinator, so the turn count is the sum of everyone's turns plus the
supervisor's own.
"""

from __future__ import annotations

from typing import Any

import topology
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
    ORIGIN_STEP_ID,
    REASON,
    REFUND_CENTS,
    KeyedRegistry,
    Trace,
    anchored_key,
)

__all__ = [
    "DELEGATE",
    "SUPERVISOR_RUN_ID",
    "WORKER_GOALS",
    "WORKER_SCRIPTS",
    "run_supervisor",
]

SUPERVISOR_RUN_ID = "run_01H8XQ6SUPERVISOR"

DELEGATE = ToolSpec(
    name="delegate_to_worker",
    description="Run one scoped subtask; return a summary.",
    input_schema={"type": "object", "properties": {
        "agent": {"enum": ["policy", "fraud"]},
        "goal": {"type": "string"},
        "turns_left": {"type": "integer"}}},
    output_schema={"type": "object"},
    writes=False,            # workers cannot mutate; only the
    idempotent=True,         # supervisor holds issue_refund
    max_result_tokens=400,   # the compression budget, enforced
)

WORKER_GOALS = {
    "policy": (
        "Policy check: what does Northstar allow for a damaged Field "
        "Speaker, and what is the approval threshold?"
    ),
    "fraud": (
        "Fraud check: order NR-2026-0042110 is flagged. Is a partial "
        "refund of one unit defensible?"
    ),
}

#: One script per worker, matched by substring on the scoped brief.
WORKER_SCRIPTS: dict[str, list[Any]] = {
    "Policy check": [
        ToolCall("p1", "get_policy", {"reason": REASON, "sku": "NR-SPEAKER-09"}),
        "Damaged goods are refundable at 100 percent within 30 days, and "
        "anything at or above 5000 cents needs a human.",
    ],
    "Fraud check": [
        ToolCall("f1", "get_order", {"order_id": ORDER_ID}),
        ToolCall("f2", "search_orders", {"flag": "fraud_review", "page": 1}),
        "The flag is a review hold, not a confirmed fraud finding. One "
        "unit at 12000 cents is defensible with an approval on file.",
    ],
}

#: What a worker may hold. Reads only, and a subset of the supervisor's set.
WORKER_TOOLS = ("get_order", "get_policy", "search_orders")


def _worker_registry(world: World) -> ToolRegistry:
    """A registry that physically cannot dispatch a write."""
    registry = ToolRegistry()
    for spec, fn in world.tools():
        if spec.name in WORKER_TOOLS and not spec.writes:
            registry.register(spec, fn)
    return registry


def _make_delegate(world: World, trace: Trace) -> Any:
    """Build the delegation tool. Each call is one isolated worker run."""

    def delegate_to_worker(
        agent: str,
        goal: str,
        turns_left: int = 4,
    ) -> dict[str, Any]:
        worker = AgentLoop(
            model=FakeModel(WORKER_SCRIPTS),
            tools=_worker_registry(world),
            telemetry=trace.tap(f"worker:{agent}"),
            max_turns=turns_left,
            budget_cents=40,
            principal=topology.principal(),
        )
        state = worker.run(goal, run_id=f"{SUPERVISOR_RUN_ID}:{agent}")
        # A summary and references. Not the worker's message list: a
        # supervisor that receives full tool results fills its context with
        # material it cannot use, and its later decisions degrade.
        return {
            "agent": agent,
            "finding": state.final_text or "",
            "evidence_refs": [f"artifact://runs/{state.run_id}"],
            "turns": state.step,
        }

    return delegate_to_worker


def _supervisor_registry(world: World, trace: Trace) -> ToolRegistry:
    """Reads, delegation, and the two writes only the supervisor holds."""
    held = {"get_order", "get_policy", "issue_refund", "send_message"}
    registry = KeyedRegistry(
        anchored_key(SUPERVISOR_RUN_ID, ORIGIN_STEP_ID)
    )
    for spec, fn in world.tools():
        if spec.name in held:
            registry.register(spec, fn)
    registry.register(DELEGATE, _make_delegate(world, trace))
    return registry


def _decide(messages: list[Message]) -> ToolCall | str:
    """The supervisor's trajectory, as a function of what it has observed."""
    state = RunState(run_id=SUPERVISOR_RUN_ID, messages=list(messages))
    seen = topology.observations(state)

    def delegated(agent: str) -> bool:
        return any(
            o.get("tool") == "delegate_to_worker"
            and o.get("ok")
            and (o.get("content") or {}).get("agent") == agent
            for o in seen
        )

    if not topology.succeeded(state, "get_order"):
        return ToolCall("s1", "get_order", {"order_id": ORDER_ID})
    for agent in ("policy", "fraud"):
        if not delegated(agent):
            return ToolCall(
                f"s-{agent}",
                "delegate_to_worker",
                {"agent": agent, "goal": WORKER_GOALS[agent], "turns_left": 4},
            )
    if not topology.succeeded(state, "issue_refund"):
        attempt = sum(1 for o in seen if o.get("tool") == "issue_refund") + 1
        return ToolCall(
            f"s-refund-{attempt}",
            "issue_refund",
            {
                "order_id": ORDER_ID,
                "amount_cents": REFUND_CENTS,
                "reason": REASON,
            },
        )
    if not topology.succeeded(state, "send_message"):
        return ToolCall(
            "s-msg",
            "send_message",
            {
                "order_id": ORDER_ID,
                "body": (
                    "We have refunded US$120.00 for the damaged Field "
                    "Speaker. The second unit is unaffected."
                ),
            },
        )
    return (
        "Refunded one Field Speaker at 12000 cents with approval on file "
        "and confirmed to the customer."
    )


def run_supervisor(world: World) -> tuple[RunState, Trace, KeyedRegistry]:
    """Run the supervisor topology against the faulted world."""
    trace = Trace()
    approvals = ApprovalStore()
    registry = _supervisor_registry(world, trace)
    loop = AgentLoop(
        model=FakeModel(default=[_decide] * 10),
        tools=registry,
        policy=topology.policy(),
        approvals=approvals,
        telemetry=trace.tap("supervisor"),
        max_turns=12,
        budget_cents=300,
        principal=topology.principal(),
    )
    state = loop.run(GOAL, run_id=SUPERVISOR_RUN_ID)
    state = topology.decide_and_resume(loop, approvals, state)
    return state, trace, registry
