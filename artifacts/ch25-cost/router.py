"""Capability routing, and a deterministic check that decides escalation.

Routing sends each step to the cheapest model that can do *that step*
reliably on *your* task set. The rule this module enforces is that the
decision to escalate is never a second model's opinion. A verifier that is
itself a model has its own error rate, and a cascade whose verifier passes
bad answers fifteen percent of the time is a cheap way to ship worse
results.

So :func:`escalate` is built from three kinds of check and no others:

* **schema** — the result has the shape the tool contract promised;
* **arithmetic** — the numbers reconcile against authoritative state;
* **lookup** — the thing the result names actually exists.

That is a narrow instrument, and its narrowness is the point. There are
failures it cannot see — a refund that is well formed, arithmetically
consistent, and applies the wrong policy — and the harness in
``compare.py`` measures exactly that gap rather than talking about it.
"""

from __future__ import annotations

from northstar_contracts import ToolResult, World

__all__ = [
    "CHEAP_STEPS",
    "MODEL_CLASSES",
    "escalate",
    "ledger_reconciles",
    "route",
    "schema_ok",
]

#: Step kinds a small model handles as well as a large one, on the
#: Northstar task set. This is an evaluation result, not an opinion: change
#: the task set and you have to re-measure it.
CHEAP_STEPS = frozenset({"lookup", "extract", "classify", "triage"})

#: The two capability classes :func:`route` chooses between.
MODEL_CLASSES = ("small", "large")


def route(step: str) -> str:
    """Cheap model for extraction and classification; large for planning."""
    return "small" if step in CHEAP_STEPS else "large"


def schema_ok(result: ToolResult) -> bool:
    """Whether the result has the shape its contract promised.

    A failed call fails this check, which is the common case in practice:
    the cheap model asked for something the tool refused, so there is a
    well-defined, side-effect-free step to redo.
    """
    if not result.ok or not isinstance(result.content, dict):
        return False
    content = result.content
    if "refund_id" not in content:
        return True
    amount = content.get("amount_cents")
    return (
        isinstance(amount, int)
        and not isinstance(amount, bool)
        and amount > 0
        and isinstance(content.get("refund_id"), str)
    )


def ledger_reconciles(result: ToolResult, world: World) -> bool:
    """Whether a refund result agrees with the authoritative ledger.

    Takes the world because a reconciliation needs an authority. A check
    that reads only the model's own output is not a check; it is a
    restatement.

    Results that are not refunds reconcile trivially, so a caller can run
    this over every observation in a turn without branching.
    """
    if not result.ok or not isinstance(result.content, dict):
        return False
    content = result.content
    refund_id = content.get("refund_id")
    if not isinstance(refund_id, str):
        return True

    order_id = str(content.get("order_id", ""))
    order = world.orders.get(order_id)
    if order is None:
        return False
    rows = [r for r in world.refunds_for(order_id) if r.refund_id == refund_id]
    if len(rows) != 1:
        return False
    if rows[0].amount_cents != content.get("amount_cents"):
        return False
    return world.total_refunded_cents(order_id) <= order["total_cents"]


def escalate(result: ToolResult, world: World) -> bool:
    """Deterministic verification only: schema, arithmetic, or lookup."""
    return not schema_ok(result) or not ledger_reconciles(result, world)
