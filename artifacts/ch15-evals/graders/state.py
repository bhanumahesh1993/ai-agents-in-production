"""Grade the world, not the run's account of itself.

The outcome is the state of the authoritative systems the agent touched, read
after the run finished, from the systems themselves. It is not the run's
status field and it is not the closing summary; both of those are claims made
by the system under test.

What outcome grading catches is the whole family of silent failures: the run
that reported success and did nothing, the run that refunded the wrong amount,
the run that refunded twice, the run whose message was drafted and never sent.
What it cannot catch is everything about *how*, which is what
``graders/trajectory.py`` is for.
"""

from __future__ import annotations

from northstar_contracts import RunState, World
from northstar_evals import GradeResult, StateGrader

__all__ = ["RefundStateGrader", "ledger_is_consistent"]


def ledger_is_consistent(world: World) -> bool:
    """Whether the side-effect ledger agrees with the refund rows.

    Three views of the same money have to match: the append-only ledger,
    the refund table, and the running total on the order. A reconciliation
    that only ever reads one of them will pass while the other two drift,
    which is the shape of the failure this book opens with.

    ``World`` does not offer this as a method, so it lives here: a grader
    is allowed to know how to reconcile the store it grades.
    """
    from_ledger: dict[str, int] = {}
    for entry in world.effects("refund_issued"):
        order_id = str(entry["order_id"])
        from_ledger[order_id] = (
            from_ledger.get(order_id, 0) + int(entry["amount_cents"])
        )
    for order_id, order in world.orders.items():
        rows = world.total_refunded_cents(order_id)
        if from_ledger.get(order_id, 0) != rows:
            return False
        if int(order["refunded_cents"]) != rows:
            return False
    return True


class RefundStateGrader(StateGrader):
    """Grade the world, not the run's account of itself.

    Args:
        order_id: The order every check is asserted on.
        expected_cents: What should have been refunded. Zero is a real
            expectation, not the absence of one: it is how a case says
            that the correct outcome was for no money to move.
        expect_escalation: Whether an open specialist case is required.
        expect_messages: How many customer-visible messages should exist,
            or ``None`` to leave message volume ungraded.
    """

    name = "state"

    def __init__(
        self,
        order_id: str,
        expected_cents: int,
        *,
        expect_escalation: bool = False,
        expect_messages: int | None = None,
    ) -> None:
        super().__init__()
        self.order_id = order_id
        self.expected_cents = expected_cents
        self.expect_escalation = expect_escalation
        self.expect_messages = expect_messages

    def grade(self, run: RunState, world: World) -> GradeResult:
        """Assert on the ledger. ``run`` is used only to attribute the result."""
        refunds = world.refunds_for(self.order_id)
        checks = {
            "single_refund": len(refunds) <= 1,
            "amount_cents": (
                sum(r.amount_cents for r in refunds) == self.expected_cents
            ),
            "ledger_consistent": ledger_is_consistent(world),
        }
        if self.expect_escalation:
            checks["escalated"] = any(
                c["order_id"] == self.order_id and c["status"] == "open"
                for c in world.escalations
            )
        if self.expect_messages is not None:
            checks["messages_sent"] = (
                len(world.messages) == self.expect_messages
            )

        failed = [name for name, ok in checks.items() if not ok]
        return GradeResult(
            passed=not failed,
            score=(len(checks) - len(failed)) / len(checks),
            reasons=[f"{name}: failed" for name in failed],
            grader=self.name,
            details={
                **checks,
                "run_id": run.run_id,
                "world": world.snapshot(),
            },
        )
