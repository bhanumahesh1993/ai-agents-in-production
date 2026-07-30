"""The portable core. The same agent, whichever adapter is handed to it.

The agent itself is untouched: the same ``AgentLoop``, the same tool
contracts, the same policy, the same graders that have been running since
Chapter 2. What the adapter supplies is the *wiring* — where sessions are
held, where the tools are reached, how the platform's inbound
authorization maps onto a principal, and where spans go.

Every deployment goes behind an auth boundary and enforces the 5,000-cent
approval threshold, because a benchmark of an agent without its policy gate
is a benchmark of a system you are not going to ship.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from northstar_contracts import RunState, ToolCall, World, idempotency_key
from northstar_evals import GradeResult, StateGrader
from northstar_policy import (
    ApprovalStore,
    Principal,
    default_northstar_policy,
)
from northstar_runtime import AgentLoop, FakeModel, ToolRegistry

from adapters.base import CloudAdapter

__all__ = [
    "APPROVAL_THRESHOLD_CENTS",
    "INBOUND",
    "Attempt",
    "run_once",
]

#: The gate every deployment enforces, identical across platforms.
APPROVAL_THRESHOLD_CENTS = 5000

#: One inbound request, in the union shape the mock adapter accepts. Each
#: real adapter reads the fields its platform actually sends; the point of
#: running them all against one fixture is that the *mapping* is what
#: differs, not the agent.
INBOUND: dict[str, Any] = {
    "user_id": "CUST-8841",
    "agent_id": "northstar-support-agent",
    "operator_id": "northstar-platform",
    "scopes": ["orders:read", "refunds:write"],
    "claims": {
        "sub": "CUST-8841",
        "act": {"sub": "northstar-support-agent"},
        "scope": "orders:read refunds:write",
    },
    "spiffe_id": "spiffe://northstar.example/agent/northstar-support-agent",
    "delegated": {
        "sub": "CUST-8841",
        "scopes": ["orders:read", "refunds:write"],
    },
    "agent_identity": {
        "app_id": "northstar-support-agent",
        "roles": ["orders:read", "refunds:write"],
    },
    "user": {"oid": "CUST-8841"},
    "developer": {"roles": ["orders:read", "refunds:write", "admin:all"]},
}


@dataclass(frozen=True)
class Attempt:
    """One task run on one adapter, graded against the world.

    ``verified`` comes from a state grader reading the authoritative store,
    never from the agent's account of itself. A platform's managed
    evaluation service will happily grade a final response; none of them
    knows what "correct" means for your refund ledger.
    """

    task: str
    cloud: str
    verified: bool
    grade: GradeResult
    duration_ms: int
    input_tokens: int
    output_tokens: int
    model: str
    status: str

    def to_dict(self) -> dict[str, Any]:
        """JSON-serialisable form."""
        return {
            "task": self.task,
            "cloud": self.cloud,
            "verified": self.verified,
            "duration_ms": self.duration_ms,
            "status": self.status,
            "reasons": list(self.grade.reasons),
        }


def build_loop(
    adapter: CloudAdapter,
    world: World,
    script: list[Any],
    approvals: ApprovalStore | None = None,
) -> AgentLoop:
    """Assemble the portable core against one adapter.

    Four adapter calls, and nothing else about the loop changes. If a fifth
    were needed the interface would have grown, which is the signal the
    chapter asks you to watch for.
    """
    principal: Principal = adapter.principal_for(INBOUND)
    return AgentLoop(
        model=FakeModel(default=script),
        tools=ToolRegistry(inject_idempotency_key=True).register_all(
            world.tools()
        ),
        checkpointer=adapter.session_store(),
        policy=default_northstar_policy(APPROVAL_THRESHOLD_CENTS),
        principal=principal,
        approvals=approvals,
        max_turns=8,
    )


def run_once(
    adapter: CloudAdapter,
    name: str,
    goal: str,
    script: list[Any],
    grader: StateGrader,
    run_id: str,
) -> Attempt:
    """Run one task on one adapter and grade the world it left.

    The duration is measured, not estimated. Against the mock adapter it is
    the harness's own overhead rather than a platform figure, which is
    exactly what the scorecard should say it is.
    """
    world = World()
    loop = build_loop(adapter, world, script)
    started = time.perf_counter()
    try:
        state = loop.run(goal, run_id=run_id)
    except Exception as exc:  # noqa: BLE001 - a failed run is a datum
        state = RunState(run_id=run_id, status="failed")
        grade = grader.grade(state, world)
        return Attempt(
            task=name,
            cloud=adapter.name,
            verified=False,
            grade=grade,
            duration_ms=int((time.perf_counter() - started) * 1000),
            input_tokens=0,
            output_tokens=0,
            model=type(exc).__name__,
            status="failed",
        )
    duration_ms = int((time.perf_counter() - started) * 1000)
    grade = grader.grade(state, world)
    usage = _usage(loop)
    return Attempt(
        task=name,
        cloud=adapter.name,
        verified=grade.passed,
        grade=grade,
        duration_ms=duration_ms,
        input_tokens=usage[0],
        output_tokens=usage[1],
        model=usage[2],
        status=state.status,
    )


def _usage(loop: AgentLoop) -> tuple[int, int, str]:
    """Token usage and model name, from the loop's own event log."""
    records = loop.events.of_type("model.called")
    return (
        sum(int(r["payload"]["input_tokens"]) for r in records),
        sum(int(r["payload"]["output_tokens"]) for r in records),
        str(records[0]["payload"]["model"]) if records else "none",
    )


def refund_script(order_id: str, amount_cents: int) -> list[Any]:
    """The trajectory every platform runs. Identical by construction."""
    return [
        ToolCall("c1", "get_order", {"order_id": order_id}),
        ToolCall("c2", "get_policy", {"reason": "damaged"}),
        ToolCall(
            "c3",
            "issue_refund",
            {
                "order_id": order_id,
                "amount_cents": amount_cents,
                "reason": "damaged",
                "idempotency_key": idempotency_key(order_id, "refund"),
            },
        ),
        f"Refunded {amount_cents} cents.",
    ]
