"""Two writers, one brief, and a world neither of them wanted.

The whiteboard architecture from the chapter's opening. Two agents get the
same brief and full tool access. Both do competent work. Both actions are
defensible in isolation. Both writes carry a derived idempotency key and
would survive any retry.

Together, Northstar has refunded a customer and promised them a free
replacement, and told them so in writing.

No merge step fixes this. The writes have already landed. A reconciling
model can compose a graceful apology, but it cannot un-send the message or
un-move the money, and the compensating action — clawing back a refund from
a customer who was told they were getting a replacement — is exactly the
kind of action you never want an agent taking.

Idempotency keys do not protect you here. A key makes the same intent safe
to execute more than once. It has nothing to say about two different
intents, each executed exactly once, that should never both have happened.
"""

from __future__ import annotations

from typing import Any

import orchestrator
from northstar_contracts import Money, RunState, ToolCall, World
from northstar_runtime import AgentLoop, FakeModel
from orchestrator import ORDER_ID, REFUND_CENTS, all_tools, lead_principal

__all__ = [
    "BRIEF",
    "WRITER_SCRIPTS",
    "WRITERS",
    "conflicts",
    "ledger_for",
    "run_writer",
]

BRIEF = "Make ticket 8812 right for the customer."

WRITERS = ("writer_a", "writer_b")

#: Attribution is a real cost of splitting an agent. The side-effect ledger
#: records what happened, not who did it, so the span each writer produced
#: has to be recorded at the boundary. Chapter 17 threads a trace parent
#: through every crossing instead, which is the version that scales.
_SPANS: dict[str, tuple[int, int]] = {}


#: Two competent readings of one under-specified brief. Neither agent is
#: wrong. The brief left "refund or replace" open, and each of them closed
#: it, in the only place a closed decision is ever recorded: the change it
#: made to the world.
WRITER_SCRIPTS: dict[str, dict[str, list[Any]]] = {
    "writer_a": {
        BRIEF: [
            ToolCall("wa1", "get_order", {"order_id": ORDER_ID}),
            ToolCall("wa2", "get_policy", {"reason": "damaged"}),
            ToolCall(
                "wa3",
                "issue_refund",
                {
                    "order_id": ORDER_ID,
                    "amount_cents": REFUND_CENTS,
                    "reason": "damaged",
                },
            ),
            "Refunded US$32.50 for the cracked lamp shade.",
        ]
    },
    "writer_b": {
        BRIEF: [
            ToolCall("wb1", "get_order", {"order_id": ORDER_ID}),
            ToolCall(
                "wb2",
                "send_message",
                {
                    "order_id": ORDER_ID,
                    "body": (
                        "Good news: a replacement lamp shade ships today at "
                        "no charge."
                    ),
                },
            ),
            ToolCall(
                "wb3",
                "escalate_to_specialist",
                {
                    "order_id": ORDER_ID,
                    "reason": "replacement_dispatch",
                    "notes": "Customer prefers a working shade to a refund.",
                },
            ),
            "Arranged a free replacement, shipping today.",
        ]
    },
}


def run_writer_state(
    world: World,
    name: str,
    budget_cents: Money = 60,
) -> RunState:
    """Run one writer against the shared world and hand back its state."""
    before = len(world.ledger)
    loop = AgentLoop(
        model=FakeModel(WRITER_SCRIPTS[name]),
        tools=all_tools(world),
        max_turns=6,
        budget_cents=budget_cents,
        principal=lead_principal(),
    )
    state = loop.run(BRIEF, run_id=f"run_ch05_{name}")
    # Every write above carries idempotency_key(run_id, step), stamped by
    # the registry. Both writers are green, both are within budget, and
    # neither key has anything to say about the other writer's intent.
    _SPANS[name] = (before, len(world.ledger))
    return state


def run_writer(
    world: World,
    name: str,
    budget_cents: Money = 60,
) -> list[dict[str, Any]]:
    """Run one writer; return the side effects it produced."""
    run_writer_state(world, name, budget_cents)
    return ledger_for(world, name)


def ledger_for(world: World, name: str) -> list[dict[str, Any]]:
    """The side-effect ledger entries one writer produced."""
    start, end = _SPANS.get(name, (0, 0))
    return world.ledger[start:end]


#: Phrases that constitute a promise of a replacement. Reading intent out of
#: prose is exactly as fragile as it looks, and that fragility is the point:
#: the decision writer_b made is recorded nowhere except in the sentence it
#: sent the customer. The escalation check below is structural because the
#: escalation carries a typed reason; the message check cannot be.
REPLACEMENT_PROMISE = (
    "replacement lamp shade",
    "free replacement",
    "reshipment",
)


def _promises_a_replacement(body: str) -> bool:
    """Whether a customer-visible message committed Northstar to a reship."""
    lowered = body.lower()
    return any(marker in lowered for marker in REPLACEMENT_PROMISE)


def conflicts(world: World, order_id: str = ORDER_ID) -> list[str]:
    """Resolutions that landed against one order and contradict each other.

    Read from the world, not from either run's account of itself. Both runs
    reported success and both traces are green; the incoherence exists only
    in the ledger, which is the only place either agent's decision was ever
    recorded.
    """
    refunded = any(
        e["kind"] == "refund_issued" and e["order_id"] == order_id
        for e in world.ledger
    )
    promised = any(
        e["kind"] == "message_sent"
        and e["order_id"] == order_id
        and _promises_a_replacement(str(e.get("body", "")))
        for e in world.ledger
    )
    dispatching = any(
        e["kind"] == "escalated"
        and e["order_id"] == order_id
        and e.get("reason") == "replacement_dispatch"
        for e in world.ledger
    )
    found: list[str] = []
    if refunded and promised:
        found.append(
            f"{order_id}: refunded and promised a free replacement in writing"
        )
    if refunded and dispatching:
        found.append(
            f"{order_id}: refunded and queued for replacement dispatch"
        )
    return found


def orchestrated_resolution(world: World) -> list[str]:
    """The same brief through the shape that reconciles before it acts."""
    orchestrator.resolve_ticket(world, BRIEF)
    return conflicts(world)
