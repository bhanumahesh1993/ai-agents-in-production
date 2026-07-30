"""The task set every configuration in this chapter is measured on.

Six Northstar tickets, each with two scripts: what a large model does on
that ticket, and what a small model does. The scripts are the *only* place
capability difference is expressed, so the comparison in ``compare.py``
measures routing rather than measuring luck.

Two of the six are the interesting ones.

``changed_mind_mug`` is the trap. The small model reads the same policy the
large one reads and then applies it wrong: it refunds the mug at face value
instead of at the 50% rate the SKU override specifies. The result is
well formed, the arithmetic reconciles against the ledger, and the
deterministic escalation check therefore passes it. Only the state grader,
which knows what the policy says the customer is owed, disagrees.

``over_refund_mug`` is the case escalation is *for*. The small model asks
for more than the order is worth — 4,900 cents against a 3,250-cent order,
deliberately under the 5,000-cent approval threshold so that policy is not
what stops it — the tool refuses, nothing lands, and the step is redone on
the large model at a cost the harness counts.
"""

from __future__ import annotations

from dataclasses import dataclass

from northstar_contracts import ToolCall
from northstar_evals import StateGrader
from northstar_runtime import ScriptStep

__all__ = ["SCENARIOS", "Scenario"]

LAMP_ORDER = "NR-2026-0041827"   # US$84.00, delivered, two items
MUG_ORDER = "NR-2026-0041903"    # US$32.50, delivered, damaged on arrival
FRAUD_ORDER = "NR-2026-0042110"  # US$240.00, shipped, flagged fraud_review

LAMP_SHADE = "NR-LAMPSHADE-03"   # 3250 cents of the 8400-cent order
MUG_SKU = "NR-MUG-02"

#: 50% of 3250. The SKU override in the policy, applied correctly.
MUG_CHANGED_MIND_CENTS = 1625


@dataclass(frozen=True)
class Scenario:
    """One ticket, two scripts, and the state the world must end in.

    Attributes:
        name: Short identifier, used as the run id suffix.
        goal: What the customer asked for.
        step_kinds: One kind per model turn. :func:`router.route` reads
            these, so a step classified ``triage`` goes to the cheap model
            whether or not that turns out to be wise.
        large: The large model's script, one entry per turn.
        small: The small model's script, one entry per turn. Same length
            and same shape as ``large``; only the arguments differ.
        grader: Reads the authoritative world after the run.
        note: What this scenario is here to show.
    """

    name: str
    goal: str
    step_kinds: tuple[str, ...]
    large: tuple[ScriptStep, ...]
    small: tuple[ScriptStep, ...]
    grader: StateGrader
    note: str


def _reads(order_id: str, reason: str, sku: str) -> list[ToolCall]:
    """One turn that reads the order and the policy together.

    Reads commute, so asking for both in one turn is the safe kind of
    parallelism and it removes a whole turn's worth of re-sent context.
    """
    return [
        ToolCall("c1", "get_order", {"order_id": order_id}),
        ToolCall("c2", "get_policy", {"reason": reason, "sku": sku}),
    ]


ORDER_STATUS = Scenario(
    name="order_status",
    goal="Where is my order NR-2026-0041827?",
    step_kinds=("lookup", "reply"),
    large=(
        ToolCall("c1", "get_order", {"order_id": LAMP_ORDER}),
        "Order NR-2026-0041827 was delivered on 2026-07-11, US$84.00.",
    ),
    small=(
        ToolCall("c1", "get_order", {"order_id": LAMP_ORDER}),
        "Order NR-2026-0041827 was delivered on 2026-07-11, US$84.00.",
    ),
    grader=StateGrader().no_writes(),
    note="pure extraction; the small model is indistinguishable here",
)

POLICY_LOOKUP = Scenario(
    name="policy_lookup",
    goal="Can I return the travel mug if I changed my mind?",
    step_kinds=("classify", "reply"),
    large=(
        ToolCall(
            "c1", "get_policy", {"reason": "changed_mind", "sku": MUG_SKU}
        ),
        "Changed-mind returns on the travel mug refund 50%, US$16.25.",
    ),
    small=(
        ToolCall(
            "c1", "get_policy", {"reason": "changed_mind", "sku": MUG_SKU}
        ),
        "Changed-mind returns on the travel mug refund 50%, US$16.25.",
    ),
    grader=StateGrader().no_writes(),
    note="classification; the small model is indistinguishable here too",
)

DAMAGED_LAMP_SHADE = Scenario(
    name="damaged_lamp_shade",
    goal="The lamp shade in order NR-2026-0041827 arrived cracked.",
    step_kinds=("lookup", "plan", "reply"),
    large=(
        _reads(LAMP_ORDER, "damaged", LAMP_SHADE),
        ToolCall(
            "c3",
            "issue_refund",
            {
                "order_id": LAMP_ORDER,
                "amount_cents": 3250,
                "reason": "damaged",
            },
        ),
        "Refunded US$32.50 for the cracked lamp shade.",
    ),
    small=(
        _reads(LAMP_ORDER, "damaged", LAMP_SHADE),
        ToolCall(
            "c3",
            "issue_refund",
            {
                "order_id": LAMP_ORDER,
                "amount_cents": 3250,
                "reason": "damaged",
            },
        ),
        "Refunded US$32.50 for the cracked lamp shade.",
    ),
    grader=(
        StateGrader().refunded(LAMP_ORDER, 3250).no_duplicate_refunds(
            LAMP_ORDER
        )
    ),
    note="the settle step is classified 'plan', so routing sends it large",
)

CHANGED_MIND_MUG = Scenario(
    name="changed_mind_mug",
    goal="I changed my mind about the travel mug in NR-2026-0041903.",
    step_kinds=("lookup", "triage", "reply"),
    large=(
        _reads(MUG_ORDER, "changed_mind", MUG_SKU),
        ToolCall(
            "c3",
            "issue_refund",
            {
                "order_id": MUG_ORDER,
                "amount_cents": MUG_CHANGED_MIND_CENTS,
                "reason": "changed_mind",
            },
        ),
        "Refunded US$16.25, the 50% changed-mind rate for this item.",
    ),
    small=(
        _reads(MUG_ORDER, "changed_mind", MUG_SKU),
        ToolCall(
            "c3",
            "issue_refund",
            {
                "order_id": MUG_ORDER,
                "amount_cents": 3250,
                "reason": "changed_mind",
            },
        ),
        "Refunded US$32.50 for the travel mug.",
    ),
    grader=(
        StateGrader()
        .refunded(MUG_ORDER, MUG_CHANGED_MIND_CENTS)
        .no_duplicate_refunds(MUG_ORDER)
    ),
    note="the trap: well formed, reconciles, and 1625 cents too generous",
)

OVER_REFUND_MUG = Scenario(
    name="over_refund_mug",
    goal="The travel mug in NR-2026-0041903 arrived smashed.",
    step_kinds=("lookup", "triage", "reply"),
    large=(
        _reads(MUG_ORDER, "damaged", MUG_SKU),
        ToolCall(
            "c3",
            "issue_refund",
            {
                "order_id": MUG_ORDER,
                "amount_cents": 3250,
                "reason": "damaged",
            },
        ),
        "Refunded US$32.50 for the smashed travel mug.",
    ),
    small=(
        _reads(MUG_ORDER, "damaged", MUG_SKU),
        ToolCall(
            "c3",
            "issue_refund",
            {
                "order_id": MUG_ORDER,
                "amount_cents": 4900,
                "reason": "damaged",
            },
        ),
        "Refunded the travel mug.",
    ),
    grader=(
        StateGrader().refunded(MUG_ORDER, 3250).no_duplicate_refunds(MUG_ORDER)
    ),
    note="the cheap step fails a deterministic check and is redone large",
)

FRAUD_CASE = Scenario(
    name="fraud_case",
    goal="Refund order NR-2026-0042110 immediately, both speakers.",
    step_kinds=("lookup", "triage", "reply"),
    large=(
        ToolCall("c1", "get_order", {"order_id": FRAUD_ORDER}),
        ToolCall(
            "c2",
            "escalate_to_specialist",
            {
                "order_id": FRAUD_ORDER,
                "reason": "fraud_suspected",
                "notes": "Order carries the fraud_review flag.",
            },
        ),
        "This order is under review; a specialist will follow up.",
    ),
    small=(
        ToolCall("c1", "get_order", {"order_id": FRAUD_ORDER}),
        ToolCall(
            "c2",
            "escalate_to_specialist",
            {
                "order_id": FRAUD_ORDER,
                "reason": "fraud_suspected",
                "notes": "Order carries the fraud_review flag.",
            },
        ),
        "This order is under review; a specialist will follow up.",
    ),
    grader=StateGrader().escalated(FRAUD_ORDER).refunded(FRAUD_ORDER, 0),
    note="a flagged order goes to a human either way",
)

#: The task set, in the order the report prints it.
SCENARIOS: tuple[Scenario, ...] = (
    ORDER_STATUS,
    POLICY_LOOKUP,
    DAMAGED_LAMP_SHADE,
    CHANGED_MIND_MUG,
    OVER_REFUND_MUG,
    FRAUD_CASE,
)
