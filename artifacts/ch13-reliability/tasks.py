"""The twelve-task Northstar critical set, bucketed by duration.

A reliability measurement is only as meaningful as the task set under it, so
these are declared as data: each :class:`Task` carries the fixtures it starts
from, the plan the model follows, and the authoritative-state expectation it
is graded against.

Two fields are the ones readers underuse.

``bucket`` is the duration slice. Chapter 13's argument is that a suite mean
built from a slice that works and a slice that does not describes neither, so
every task declares which slice it is in and the report prints the buckets
separately. Short tasks need six turns or fewer; long tasks need twelve or
more.

``plan`` is a :class:`Plan` rather than a positional script. The difference
matters for the measurement. A positional script loses a step every time the
flaky wrapper wastes a turn, so *any* interference is fatal and every task
decays at the same rate. A plan re-derives the next action from what has
actually succeeded, which is what a competent agent does, so a wasted turn
costs a turn and not a step. Reliability then falls with duration, because a
long task has more draws against the same turn ceiling -- which is the effect
the chapter is about.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

from northstar_contracts import Message, ToolCall, ToolSpec, World
from northstar_evals import StateGrader

ORDER = "NR-2026-0041827"          # US$84.00, delivered, two items
MUG_ORDER = "NR-2026-0041903"      # US$32.50, delivered, flagged damaged
FRAUD_ORDER = "NR-2026-0042110"    # US$240.00, shipped, fraud_review
CUSTOMER = "CUST-8841"

HEADPHONES = "NR-HEADPHONES-01"
HEADPHONES_CENTS = 5150
LAMP_SHADE = "NR-LAMPSHADE-03"
LAMP_SHADE_CENTS = 3250
MUG = "NR-MUG-02"
MUG_CENTS = 3250
MUG_CHANGED_MIND_CENTS = 1625      # the 50% SKU override in the policy table

#: Extra turns every task gets beyond the length of its plan. One constant
#: for the whole suite, so the duration effect the report shows comes from
#: the plans differing in length and not from the harness being kinder to
#: one bucket than the other.
TURN_SLACK = 2

ToolBinding = tuple[ToolSpec, Callable[..., Any]]


def _executed(
    messages: Sequence[Message],
) -> list[tuple[ToolCall, bool]]:
    """Every tool call the model made, paired with whether it succeeded.

    Reconstructed from the conversation rather than tracked on the side,
    for the same reason ``northstar_evals.trajectory`` does it: a record
    kept in a side channel disagrees with the transcript after the first
    restart.
    """
    outcomes: dict[str, bool] = {}
    for message in messages:
        if message.role == "tool" and isinstance(message.content, dict):
            call_id = str(message.content.get("call_id", ""))
            outcomes[call_id] = bool(message.content.get("ok"))

    executed: list[tuple[ToolCall, bool]] = []
    for message in messages:
        for call in message.tool_calls:
            if call.id in outcomes:
                executed.append((call, outcomes[call.id]))
    return executed


@dataclass(frozen=True)
class Plan:
    """An ordered plan the scripted model works through.

    Args:
        steps: The calls, in the order a competent agent would make them.
            No two adjacent steps are identical, which is the invariant
            :meth:`cursor` rests on: the flaky model's verbatim repeat of
            the call it just made can never be read as the next step.
        final: The closing message, produced once every step has landed.

    A :class:`Plan` is callable, which is how
    :class:`~northstar_runtime.providers.FakeModel` consumes it: the script
    is this one object repeated, and on each turn it reports the next step
    that has not yet succeeded.
    """

    steps: tuple[ToolCall, ...]
    final: str

    @property
    def turns(self) -> int:
        """Turns a clean run needs: one per step, plus the final answer."""
        return len(self.steps) + 1

    def cursor(self, messages: Sequence[Message]) -> int:
        """How many plan steps have already succeeded, in order."""
        index = 0
        for call, ok in _executed(messages):
            if index >= len(self.steps):
                break
            wanted = self.steps[index]
            if (
                ok
                and call.name == wanted.name
                and call.arguments == wanted.arguments
            ):
                index += 1
        return index

    def __call__(self, messages: Sequence[Message]) -> ToolCall | str:
        """The next action: the first step that has not landed, or the reply."""
        index = self.cursor(messages)
        if index < len(self.steps):
            return self.steps[index]
        return self.final


@dataclass(frozen=True)
class Task:
    """One measurable unit of Northstar work.

    Attributes:
        id: Stable identifier, printed in the report and used to pair two
            versions in a comparison.
        goal: The opening customer message.
        bucket: ``"short"`` or ``"long"``. Chapter 13 buckets by duration
            before choosing a launch scope.
        plan: The trajectory a clean run takes. Wrapped in a
            ``FlakyModel`` by the harness, so the *planned* path is the
            happy path and the variance comes from the wrapper.
        expected: Builds the state grader for this task. A callable rather
            than a value so each run grades against a fresh grader.
        tool_names: Which of the world's tools this task may call. A task
            that cannot reach ``issue_refund`` cannot pass a refund check,
            which is how the read-only cases stay honest.
        faults: ``(tool, kind)`` pairs injected into every fresh world.
    """

    id: str
    goal: str
    bucket: str
    plan: Plan
    expected: Callable[[], StateGrader]
    tool_names: frozenset[str] = frozenset(
        {"get_order", "get_policy", "search_orders", "issue_refund",
         "send_message", "escalate_to_specialist"}
    )
    faults: tuple[tuple[str, str], ...] = field(default_factory=tuple)

    @property
    def max_turns(self) -> int:
        """The turn ceiling this task runs under.

        Derived from the plan's own length plus :data:`TURN_SLACK`, so a
        long task gets more chances to waste a turn against the same
        absolute tolerance. That is the whole mechanism behind the bucketed
        table the demo prints, and it is why the buckets differ without the
        harness ever being told which bucket a task is in.
        """
        return self.plan.turns + TURN_SLACK

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


#: The read-only slice, for tasks whose correct outcome is that nothing moved.
READ_ONLY = frozenset({"get_order", "get_policy", "search_orders"})


def _call(index: int, name: str, arguments: dict[str, Any]) -> ToolCall:
    """One plan step, with an id derived from its position."""
    return ToolCall(id=f"s{index:02d}", name=name, arguments=arguments)


def _plan(*steps: tuple[str, dict[str, Any]], final: str) -> Plan:
    """Build a :class:`Plan` from ``(tool, arguments)`` pairs."""
    return Plan(
        steps=tuple(
            _call(i, name, args) for i, (name, args) in enumerate(steps)
        ),
        final=final,
    )


def _short_refund_plan(
    order: str,
    sku: str,
    cents: int,
    reason: str = "damaged",
) -> Plan:
    """Read the order, check the rule, refund the item, tell the customer."""
    body = f"Refunded {cents} cents. Sorry about that."
    return _plan(
        ("get_order", {"order_id": order}),
        ("get_policy", {"reason": reason, "sku": sku}),
        ("issue_refund", {"order_id": order, "amount_cents": cents,
                          "reason": reason}),
        ("send_message", {"order_id": order, "body": body}),
        final=f"Refunded {cents} cents for the {reason} item.",
    )


def _long_refund_plan(
    order: str,
    sibling: str,
    sku: str,
    cents: int,
    reason: str = "damaged",
) -> Plan:
    """The same outcome over eleven steps: page, read, cross-check, verify.

    Longer trajectories are where reliability falls, and the reason here is
    mechanical rather than mysterious. Every extra turn is another draw
    against the run's ceiling, and the flaky wrapper only has to waste
    :data:`TURN_SLACK` of them before a correct plan runs out of room.
    """
    body = f"Refunded {cents} cents. Sorry about that."
    return _plan(
        ("search_orders", {"customer_id": CUSTOMER}),
        ("search_orders", {"customer_id": CUSTOMER, "page": 2}),
        ("get_order", {"order_id": order}),
        ("get_policy", {"reason": reason, "sku": sku}),
        ("get_policy", {"reason": reason}),
        ("get_order", {"order_id": sibling}),
        ("search_orders", {"customer_id": CUSTOMER, "status": "delivered"}),
        ("issue_refund", {"order_id": order, "amount_cents": cents,
                          "reason": reason}),
        ("get_order", {"order_id": order}),
        ("send_message", {"order_id": order, "body": body}),
        ("search_orders", {"customer_id": CUSTOMER,
                           "flag": "damaged_on_arrival"}),
        final=f"Checked the account, refunded {cents} cents, and confirmed it.",
    )


def _refund_expectation(order: str, cents: int) -> Callable[[], StateGrader]:
    """One refund of ``cents`` against ``order``, and exactly one message."""
    return lambda: (
        StateGrader()
        .refunded(order, cents)
        .no_duplicate_refunds(order)
        .messages_sent(1)
    )


CRITICAL_SET: tuple[Task, ...] = (
    # ---------------------------------------- short: six turns or fewer
    Task(
        id="damaged-single-item",
        goal="The lamp shade in my order arrived cracked.",
        bucket="short",
        plan=_short_refund_plan(ORDER, LAMP_SHADE, LAMP_SHADE_CENTS),
        expected=_refund_expectation(ORDER, LAMP_SHADE_CENTS),
    ),
    Task(
        id="damaged-other-order",
        goal="My travel mug turned up damaged.",
        bucket="short",
        plan=_short_refund_plan(MUG_ORDER, MUG, MUG_CENTS),
        expected=_refund_expectation(MUG_ORDER, MUG_CENTS),
    ),
    Task(
        id="read-only-status",
        goal="Where is my order, has it shipped yet?",
        bucket="short",
        plan=_plan(
            ("get_order", {"order_id": FRAUD_ORDER}),
            final="It shipped on 24 July and is on its way.",
        ),
        expected=lambda: StateGrader().no_writes(),
        tool_names=READ_ONLY,
    ),
    Task(
        id="wrong-item-named",
        goal="One of my orders is damaged, I think it is the mug.",
        bucket="short",
        plan=_plan(
            ("search_orders", {"customer_id": CUSTOMER}),
            ("get_order", {"order_id": MUG_ORDER}),
            ("get_policy", {"reason": "damaged", "sku": MUG}),
            ("issue_refund", {"order_id": MUG_ORDER,
                              "amount_cents": MUG_CENTS,
                              "reason": "damaged"}),
            ("send_message", {"order_id": MUG_ORDER,
                              "body": "Refunded the mug, sorry about that."}),
            final="Found the right order and refunded the mug.",
        ),
        expected=_refund_expectation(MUG_ORDER, MUG_CENTS),
    ),
    Task(
        id="apology-no-refund",
        goal="I changed my mind about the headphones, can I send them back?",
        bucket="short",
        plan=_plan(
            ("get_order", {"order_id": ORDER}),
            ("get_policy", {"reason": "changed_mind", "sku": HEADPHONES}),
            ("send_message", {"order_id": ORDER,
                              "body": "Here is how the return window works."}),
            final="Explained the change-of-mind window; no refund issued yet.",
        ),
        expected=lambda: (
            StateGrader().refunded(ORDER, 0).messages_sent(1)
        ),
    ),
    Task(
        id="escalate-fraud-review",
        goal="I want a refund on the speakers, they never arrived.",
        bucket="short",
        plan=_plan(
            ("get_order", {"order_id": FRAUD_ORDER}),
            ("get_policy", {"reason": "fraud_suspected"}),
            ("escalate_to_specialist", {"order_id": FRAUD_ORDER,
                                        "reason": "fraud_review"}),
            final="Handed this to a specialist because the order is flagged.",
        ),
        expected=lambda: (
            StateGrader().escalated(FRAUD_ORDER).refunded(FRAUD_ORDER, 0)
        ),
    ),
    # ------------------------------------ long: twelve turns or more
    Task(
        id="damaged-two-items",
        goal="Something in my two-item order is broken, I need to check which.",
        bucket="long",
        plan=_long_refund_plan(
            ORDER, MUG_ORDER, LAMP_SHADE, LAMP_SHADE_CENTS
        ),
        expected=_refund_expectation(ORDER, LAMP_SHADE_CENTS),
    ),
    Task(
        id="over-threshold-approval",
        goal="Refund the whole 84 dollars, the box arrived crushed.",
        bucket="long",
        plan=_plan(
            ("search_orders", {"customer_id": CUSTOMER}),
            ("get_order", {"order_id": ORDER}),
            ("get_policy", {"reason": "damaged", "sku": HEADPHONES}),
            ("get_policy", {"reason": "damaged", "sku": LAMP_SHADE}),
            ("get_policy", {"reason": "damaged"}),
            ("search_orders", {"customer_id": CUSTOMER, "page": 2}),
            ("get_order", {"order_id": MUG_ORDER}),
            ("search_orders", {"customer_id": CUSTOMER,
                               "status": "delivered"}),
            ("escalate_to_specialist",
             {"order_id": ORDER, "reason": "above_approval_threshold"}),
            ("send_message", {"order_id": ORDER,
                              "body": "A colleague will confirm this refund."}),
            ("get_order", {"order_id": ORDER}),
            final=(
                "8400 cents is over the 5000-cent threshold, so a human "
                "decides."
            ),
        ),
        # The point of the case: the correct outcome is that no money moved.
        expected=lambda: (
            StateGrader()
            .refunded(ORDER, 0)
            .escalated(ORDER)
            .messages_sent(1)
        ),
    ),
    Task(
        id="policy-ambiguous-sku",
        goal="I do not want the travel mug after all. What can you do?",
        bucket="long",
        plan=_plan(
            ("search_orders", {"customer_id": CUSTOMER}),
            ("get_order", {"order_id": MUG_ORDER}),
            ("get_policy", {"reason": "changed_mind"}),
            ("get_policy", {"reason": "changed_mind", "sku": MUG}),
            ("get_policy", {"reason": "damaged", "sku": MUG}),
            ("search_orders", {"customer_id": CUSTOMER, "page": 2}),
            ("get_order", {"order_id": ORDER}),
            ("issue_refund", {"order_id": MUG_ORDER,
                              "amount_cents": MUG_CHANGED_MIND_CENTS,
                              "reason": "changed_mind"}),
            ("get_order", {"order_id": MUG_ORDER}),
            ("send_message", {"order_id": MUG_ORDER,
                              "body": "Refunded 1625 cents under the "
                                      "change-of-mind rule."}),
            ("search_orders", {"customer_id": CUSTOMER,
                               "status": "delivered"}),
            final="The mug carries a 50% change-of-mind rule, so 1625 cents.",
        ),
        expected=_refund_expectation(MUG_ORDER, MUG_CHANGED_MIND_CENTS),
    ),
    Task(
        id="refund-after-timeout",
        goal="My lamp shade arrived cracked and the refund page errored.",
        bucket="long",
        plan=_long_refund_plan(
            ORDER, MUG_ORDER, LAMP_SHADE, LAMP_SHADE_CENTS
        ),
        expected=_refund_expectation(ORDER, LAMP_SHADE_CENTS),
        # The Chapter 1 fault: the write commits, then the call raises.
        # This is the recovery row of the capability/consistency/recovery
        # hierarchy, and it passes only because the harness stamps a
        # derived idempotency key before dispatching.
        faults=(("issue_refund", "timeout"),),
    ),
    Task(
        id="search-then-refund",
        goal="I have several orders open and one of them came damaged.",
        bucket="long",
        plan=_long_refund_plan(MUG_ORDER, ORDER, MUG, MUG_CENTS),
        expected=_refund_expectation(MUG_ORDER, MUG_CENTS),
    ),
    Task(
        id="repeat-claim-same-order",
        goal="I already reported the cracked lamp shade last week.",
        bucket="long",
        plan=_plan(
            ("search_orders", {"customer_id": CUSTOMER,
                               "flag": "damaged_on_arrival"}),
            ("get_order", {"order_id": ORDER}),
            ("search_orders", {"customer_id": CUSTOMER}),
            ("get_policy", {"reason": "damaged", "sku": LAMP_SHADE}),
            ("get_order", {"order_id": MUG_ORDER}),
            ("get_policy", {"reason": "damaged"}),
            ("search_orders", {"customer_id": CUSTOMER, "page": 2}),
            ("issue_refund", {"order_id": ORDER,
                              "amount_cents": LAMP_SHADE_CENTS,
                              "reason": "damaged"}),
            ("get_order", {"order_id": ORDER}),
            ("send_message", {"order_id": ORDER,
                              "body": "Refunded 3250 cents, only once."}),
            ("search_orders", {"customer_id": CUSTOMER,
                               "status": "delivered"}),
            final="Checked for an earlier claim, then refunded once.",
        ),
        expected=_refund_expectation(ORDER, LAMP_SHADE_CENTS),
    ),
)


def by_bucket(bucket: str) -> tuple[Task, ...]:
    """The subset of the critical set in one duration bucket."""
    return tuple(t for t in CRITICAL_SET if t.bucket == bucket)


def by_id(task_id: str) -> Task:
    """One task by id.

    Raises:
        KeyError: If no task carries that id.
    """
    for task in CRITICAL_SET:
        if task.id == task_id:
            return task
    known = ", ".join(t.id for t in CRITICAL_SET)
    raise KeyError(f"no task {task_id!r}; the critical set holds: {known}")
