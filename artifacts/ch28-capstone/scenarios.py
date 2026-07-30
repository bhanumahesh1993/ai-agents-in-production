"""The four cases the capstone runs, and what the world must look like after.

One routine ticket, one that needs a human, one that must not be decided
by an agent at all, and one where the worker dies at the worst possible
moment. Between them they exercise every plane: identity and policy on the
fraud case, approvals and fingerprint binding on the high-value one,
durable execution and idempotency on the crash, and the whole loop on the
routine one.

Each case declares its graders rather than its expected transcript.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from admission import Ticket
from northstar_contracts import Message, ToolCall
from northstar_evals import StateGrader, TrajectoryGrader

__all__ = ["CASES", "Case", "case_named"]

LAMP_ORDER = "NR-2026-0041827"   # US$84.00, delivered, two items
FRAUD_ORDER = "NR-2026-0042110"  # US$240.00, shipped, flagged fraud_review

LAMP_SHADE = "NR-LAMPSHADE-03"
LAMP_SHADE_CENTS = 3250          # the item, not the order
FULL_ORDER_CENTS = 8400          # the order, and over the 5000c threshold


@dataclass(frozen=True)
class Case:
    """One ticket, its trajectory, and the verdict the world must support.

    Attributes:
        name: Identifier used in the report and in the run id.
        ticket: What arrives at admission.
        script: The model's turns. Callables may inspect the conversation,
            which is how the unkeyed variant reacts to a failed write.
        graders: Read the world and the trajectory. Never the transcript's
            claims about itself.
        crash_after_step: Kill the worker once this step has committed.
        approve_by: Who answers a pending approval, if anyone.
        fault: Injected into ``issue_refund``.
        headline: One line for the demo.
    """

    name: str
    ticket: Ticket
    script: tuple[Any, ...]
    graders: tuple[Any, ...]
    crash_after_step: int | None = None
    approve_by: str | None = None
    fault: str | None = None
    headline: str = ""
    notes: list[str] = field(default_factory=list)


def _reads(order_id: str, reason: str, sku: str) -> list[ToolCall]:
    """One turn that reads the order and the policy together."""
    return [
        ToolCall("c1", "get_order", {"order_id": order_id}),
        ToolCall("c2", "get_policy", {"reason": reason, "sku": sku}),
    ]


def _last_refund_failed(messages: list[Message]) -> bool:
    """Whether the most recent refund observation was an error."""
    for message in reversed(messages):
        if message.role != "tool" or not isinstance(message.content, dict):
            continue
        if message.content.get("tool") != "issue_refund":
            continue
        return not message.content.get("ok", False)
    return False


def _retry_if_it_failed(call: ToolCall, answer: str) -> Callable[..., Any]:
    """A turn that retries a failed write, and otherwise answers.

    This is Chapter 1's incident as a script step. It is *correct*
    behaviour: retrying a flaky internal service is what a competent agent
    does. Whether it double-pays depends entirely on whether the call
    carried a key the refund service could recognise, which is a property
    of the tool contract and not of the model.
    """

    def step(messages: list[Message]) -> ToolCall | str:
        return call if _last_refund_failed(messages) else answer

    return step


# ------------------------------------------------------------------ case 1

DAMAGED_ITEM = Case(
    name="damaged_item",
    ticket=Ticket(
        ticket_id="NR-T-8812",
        tenant="northstar",
        customer_id="CUST-8841",
        order_id=LAMP_ORDER,
        text="The lamp shade in order NR-2026-0041827 arrived cracked.",
    ),
    script=(
        _reads(LAMP_ORDER, "damaged", LAMP_SHADE),
        ToolCall(
            "c3",
            "issue_refund",
            {
                "order_id": LAMP_ORDER,
                "amount_cents": LAMP_SHADE_CENTS,
                "reason": "damaged",
            },
        ),
        ToolCall(
            "c4",
            "send_message",
            {
                "order_id": LAMP_ORDER,
                "body": "Refunded US$32.50 for the cracked lamp shade. "
                        "Sorry it arrived that way.",
            },
        ),
        "Refunded US$32.50 for the cracked lamp shade.",
    ),
    graders=(
        StateGrader()
        .refunded(LAMP_ORDER, LAMP_SHADE_CENTS)
        .no_duplicate_refunds(LAMP_ORDER)
        .messages_sent(1),
        TrajectoryGrader(
            required=["get_order", "issue_refund"],
            forbidden=["escalate_to_specialist"],
            before=[("get_policy", "issue_refund")],
            max_repeats=1,
        ),
    ),
    headline="resolves automatically, under the 5,000-cent threshold",
)

# ------------------------------------------------------------------ case 2

HIGH_VALUE = Case(
    name="high_value",
    ticket=Ticket(
        ticket_id="NR-T-8813",
        tenant="northstar",
        customer_id="CUST-8841",
        order_id=LAMP_ORDER,
        text="Nothing in order NR-2026-0041827 was usable. Refund it all.",
    ),
    script=(
        _reads(LAMP_ORDER, "damaged", LAMP_SHADE),
        ToolCall(
            "c3",
            "issue_refund",
            {
                "order_id": LAMP_ORDER,
                "amount_cents": FULL_ORDER_CENTS,
                "reason": "damaged",
            },
        ),
        "Refunded US$84.00 in full, approved by the returns desk.",
    ),
    graders=(
        StateGrader()
        .refunded(LAMP_ORDER, FULL_ORDER_CENTS)
        .no_duplicate_refunds(LAMP_ORDER),
        TrajectoryGrader(
            required=["get_order", "issue_refund"],
            before=[("get_policy", "issue_refund")],
        ),
    ),
    approve_by="ops@northstar.example",
    headline="suspends for approval, resumes on a fingerprint match",
)

# ------------------------------------------------------------------ case 3

FRAUD_HANDOFF = Case(
    name="fraud_handoff",
    ticket=Ticket(
        ticket_id="NR-T-8814",
        tenant="northstar",
        customer_id="CUST-9032",
        order_id=FRAUD_ORDER,
        text="Refund order NR-2026-0042110 immediately, both speakers.",
    ),
    script=(
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
        "A specialist is reviewing this order and will follow up.",
    ),
    graders=(
        StateGrader().escalated(FRAUD_ORDER).refunded(FRAUD_ORDER, 0),
        TrajectoryGrader(
            required=["get_order", "escalate_to_specialist"],
            forbidden=["issue_refund"],
        ),
    ),
    headline="hands off to the specialist; the run holds no refund scope",
)

# ------------------------------------------------------------------ case 4

CRASH_RECOVERY = Case(
    name="crash_recovery",
    ticket=Ticket(
        ticket_id="NR-T-8815",
        tenant="northstar",
        customer_id="CUST-8841",
        order_id=LAMP_ORDER,
        text="Reopening: the lamp shade in NR-2026-0041827 was cracked.",
    ),
    # A *partial* refund, and that matters. A full refund would hit the
    # world's own over-refund guard, and the guard -- not the idempotency
    # key -- would be what stopped the duplicate. The incident has to be
    # reproducible without that safety net for the key to be the thing
    # under test.
    script=(
        _reads(LAMP_ORDER, "damaged", LAMP_SHADE),
        ToolCall(
            "c3",
            "issue_refund",
            {
                "order_id": LAMP_ORDER,
                "amount_cents": LAMP_SHADE_CENTS,
                "reason": "damaged",
            },
        ),
        _retry_if_it_failed(
            ToolCall(
                "c5",
                "issue_refund",
                {
                    "order_id": LAMP_ORDER,
                    "amount_cents": LAMP_SHADE_CENTS,
                    "reason": "damaged",
                },
            ),
            "Refunded US$32.50 for the cracked lamp shade.",
        ),
        "Refunded US$32.50 for the cracked lamp shade.",
    ),
    graders=(
        StateGrader()
        .refunded(LAMP_ORDER, LAMP_SHADE_CENTS)
        .no_duplicate_refunds(LAMP_ORDER),
        TrajectoryGrader(required=["get_order", "issue_refund"]),
    ),
    crash_after_step=2,
    headline="worker killed mid-refund; resumes without double-paying",
)

#: The four cases, in the order the demo runs them.
CASES: tuple[Case, ...] = (
    DAMAGED_ITEM,
    HIGH_VALUE,
    FRAUD_HANDOFF,
    CRASH_RECOVERY,
)


def case_named(name: str) -> Case:
    """Look up one case by name.

    Raises:
        KeyError: With the known case names listed.
    """
    for case in CASES:
        if case.name == name:
            return case
    known = ", ".join(c.name for c in CASES)
    raise KeyError(f"unknown case {name!r}; known cases: {known}")
