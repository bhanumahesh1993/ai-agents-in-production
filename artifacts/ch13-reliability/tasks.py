"""The Northstar critical task set, bucketed by duration.

A reliability measurement is only as meaningful as the task set under it, so
these are declared as data: each :class:`Task` carries the fixtures it starts
from, the script the model follows, and the authoritative-state expectation it
is graded against.

The ``bucket`` field is the one readers underuse. Chapter 13's argument is that
a suite mean built from a slice that works and a slice that does not describes
neither, so every task declares which slice it is in and the report prints the
buckets separately.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

from northstar_contracts import ToolCall, ToolSpec, World
from northstar_evals import StateGrader

ORDER = "NR-2026-0041827"          # US$84.00, delivered, two items
OTHER_ORDER = "NR-2026-0041903"    # US$32.50, delivered, flagged damaged
FRAUD_ORDER = "NR-2026-0042110"    # US$240.00, shipped, fraud_review
LAMP_SHADE = "NR-LAMPSHADE-03"
LAMP_SHADE_CENTS = 3250
MUG = "NR-MUG-02"
MUG_CENTS = 3250

ToolBinding = tuple[ToolSpec, Callable[..., object]]


@dataclass(frozen=True)
class Task:
    """One measurable unit of Northstar work.

    Attributes:
        id: Stable identifier, printed in the report and used to pair two
            versions in a comparison.
        goal: The opening customer message.
        bucket: ``"short"`` or ``"long"``. Chapter 13 buckets by duration
            before choosing a launch scope.
        script: The trajectory the base model follows. Wrapped in a
            ``FlakyModel`` by the harness, so the *scripted* path is the
            happy path and the variance comes from the wrapper.
        expected: Builds the state grader for this task. A callable rather
            than a value so each run grades against a fresh grader.
        tool_names: Which of the world's tools this task may call. A task
            that cannot reach ``issue_refund`` cannot pass a refund check,
            which is how the read-only cases stay honest.
    """

    id: str
    goal: str
    bucket: str
    script: Sequence[object]
    expected: Callable[[], StateGrader]
    tool_names: frozenset[str] = frozenset(
        {"get_order", "get_policy", "search_orders", "issue_refund",
         "send_message", "escalate_to_specialist"}
    )
    faults: tuple[tuple[str, str], ...] = field(default_factory=tuple)

    def build_world(self) -> World:
        """A fresh world, with this task's faults injected.

        Called once per repetition. Reusing one world across repetitions is
        the harness bug Chapter 13 warns about: run 2 would start with run
        1's refund already in the ledger and a correct agent would score
        near zero. ``test_ch13.py`` asserts that failure mode explicitly.
        """
        world = World()
        for tool, kind in self.faults:
            world.inject_fault(tool, kind=kind)
        return world

    def tools(self, world: World) -> list[ToolBinding]:
        """This task's allowed slice of the world's tool surface."""
        return [
            (spec, fn)
            for spec, fn in world.tools()
            if spec.name in self.tool_names
        ]


def _refund_script(order: str, sku: str, cents: int) -> list[object]:
    """Read the order, check policy, refund the one item, reply."""
    return [
        ToolCall("c1", "get_order", {"order_id": order}),
        ToolCall("c2", "get_policy", {"sku": sku, "reason": "damaged"}),
        ToolCall("c3", "issue_refund", {"order_id": order,
                                        "amount_cents": cents,
                                        "reason": "damaged"}),
        ToolCall("c4", "send_message", {"order_id": order,
                                        "body": "Refunded, sorry for that."}),
        "Refunded the damaged item and let the customer know.",
    ]


def _long_refund_script(order: str, sku: str, cents: int) -> list[object]:
    """The same outcome over more turns: search, read, re-read, refund.

    Longer trajectories are where reliability falls, and the reason is
    mechanical rather than mysterious: every extra turn is another draw
    from the model's failure distribution.
    """
    return [
        ToolCall("c1", "search_orders", {"customer_id": "CUST-8841"}),
        ToolCall("c2", "get_order", {"order_id": order}),
        ToolCall("c3", "get_policy", {"sku": sku, "reason": "damaged"}),
        ToolCall("c4", "get_order", {"order_id": order}),
        ToolCall("c5", "issue_refund", {"order_id": order,
                                        "amount_cents": cents,
                                        "reason": "damaged"}),
        ToolCall("c6", "get_order", {"order_id": order}),
        ToolCall("c7", "send_message", {"order_id": order,
                                        "body": "Refunded, sorry for that."}),
        "Confirmed the refund landed and told the customer.",
    ]


CRITICAL_SET: tuple[Task, ...] = (
    Task(
        id="damaged-single-item",
        goal="The lamp shade in my order arrived cracked.",
        bucket="short",
        script=_refund_script(ORDER, LAMP_SHADE, LAMP_SHADE_CENTS),
        expected=lambda: (
            StateGrader()
            .refunded(ORDER, LAMP_SHADE_CENTS)
            .no_duplicate_refunds(ORDER)
            .messages_sent(1)
        ),
    ),
    Task(
        id="damaged-other-order",
        goal="My travel mug turned up damaged.",
        bucket="short",
        script=_refund_script(OTHER_ORDER, MUG, MUG_CENTS),
        expected=lambda: (
            StateGrader()
            .refunded(OTHER_ORDER, MUG_CENTS)
            .no_duplicate_refunds(OTHER_ORDER)
            .messages_sent(1)
        ),
    ),
    Task(
        id="read-only-status",
        goal="Where is my order, has it shipped yet?",
        bucket="short",
        script=[
            ToolCall("c1", "get_order", {"order_id": FRAUD_ORDER}),
            "It shipped on 24 July and is on its way.",
        ],
        expected=lambda: StateGrader().no_writes(),
        tool_names=frozenset({"get_order", "get_policy", "search_orders"}),
    ),
    Task(
        id="damaged-two-items-long",
        goal="Something in my order is broken, I need to check which.",
        bucket="long",
        script=_long_refund_script(ORDER, LAMP_SHADE, LAMP_SHADE_CENTS),
        expected=lambda: (
            StateGrader()
            .refunded(ORDER, LAMP_SHADE_CENTS)
            .no_duplicate_refunds(ORDER)
            .messages_sent(1)
        ),
    ),
    Task(
        id="refund-after-timeout-long",
        goal="My lamp shade arrived cracked and the refund page errored.",
        bucket="long",
        script=_long_refund_script(ORDER, LAMP_SHADE, LAMP_SHADE_CENTS),
        expected=lambda: (
            StateGrader()
            .refunded(ORDER, LAMP_SHADE_CENTS)
            .no_duplicate_refunds(ORDER)
        ),
        # The Chapter 1 fault: the write commits, then the call raises.
        faults=(("issue_refund", "timeout"),),
    ),
    Task(
        id="escalate-fraud-review-long",
        goal="I want a refund on the speakers, they never arrived.",
        bucket="long",
        script=[
            ToolCall("c1", "get_order", {"order_id": FRAUD_ORDER}),
            ToolCall("c2", "get_policy", {"reason": "not_received"}),
            ToolCall("c3", "escalate_to_specialist",
                     {"order_id": FRAUD_ORDER, "reason": "fraud_review"}),
            "Handed this to a specialist because the order is flagged.",
        ],
        expected=lambda: (
            StateGrader()
            .escalated(FRAUD_ORDER)
            .refunded(FRAUD_ORDER, 0)
        ),
    ),
)


def by_bucket(bucket: str) -> tuple[Task, ...]:
    """The subset of the critical set in one duration bucket."""
    return tuple(t for t in CRITICAL_SET if t.bucket == bucket)
