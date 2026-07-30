"""Turn a measured rate and a weekly volume into a launch decision.

A reliability number on its own settles nothing. The question a launch review
is actually asking is whether the failures a measured rate implies fit inside
the failures the service level objective allows, and that is arithmetic:

    expected failures = volume * (1 - measured rate)
    budget            = volume * (1 - objective)

The interesting move is the one Chapter 13 makes with these two lines. Run
them on the whole suite and Northstar's queue is nowhere near its budget. Run
them on the short bucket alone, against that bucket's share of the volume,
and it fits. The measurement did not just describe the agent; it picked the
launch scope.

One refinement agent budgets need that ordinary service budgets do not:
failures are not interchangeable. :class:`ErrorBudget` therefore takes a
``severity`` label, and the money-moving class is expected to sit at or near
zero and to be held there by mechanism -- an idempotency key and a policy
check -- rather than by statistics.
"""

from __future__ import annotations

from dataclasses import dataclass

__all__ = ["BudgetLine", "ErrorBudget"]


@dataclass(frozen=True)
class BudgetLine:
    """One measured rate assessed against one budget.

    Attributes:
        label: What was measured, for the printed table.
        rate: The measured success rate, in ``[0, 1]``.
        volume: Items per period this rate would be applied to.
        expected_failures: ``volume * (1 - rate)``.
        budget_failures: ``volume * (1 - objective)``.
    """

    label: str
    rate: float
    volume: int
    expected_failures: float
    budget_failures: float

    @property
    def inside_budget(self) -> bool:
        """Whether the expected failures fit in the allowance."""
        return self.expected_failures <= self.budget_failures

    @property
    def headroom(self) -> float:
        """Failures of slack, negative when the budget is already gone."""
        return self.budget_failures - self.expected_failures

    def verdict(self) -> str:
        """One word for the table."""
        return "inside budget" if self.inside_budget else "over budget"


@dataclass(frozen=True)
class ErrorBudget:
    """A service level objective over a period's volume.

    Args:
        objective: Target verified task success rate, in ``(0, 1]``.
        volume: Items the period is expected to carry.
        severity: Which failure class this budget governs. Carried rather
            than used, because the arithmetic is identical and the number
            you are willing to accept is not: an unnecessary escalation
            costs a specialist a few minutes, and a duplicate refund costs
            money, trust, and a reconciliation.
    """

    objective: float
    volume: int
    severity: str = "any"

    def __post_init__(self) -> None:
        if not 0.0 < self.objective <= 1.0:
            raise ValueError(
                f"objective must be in (0, 1], got {self.objective}"
            )
        if self.volume < 0:
            raise ValueError(f"volume must not be negative, got {self.volume}")

    @property
    def budget_failures(self) -> float:
        """Failures the period may absorb before autonomy stops widening."""
        return self.volume * (1.0 - self.objective)

    def expected_failures(self, rate: float) -> float:
        """Failures a measured success rate implies over this volume."""
        if not 0.0 <= rate <= 1.0:
            raise ValueError(f"rate must be in [0, 1], got {rate}")
        return self.volume * (1.0 - rate)

    def assess(self, label: str, rate: float) -> BudgetLine:
        """Assess one measured rate against this budget."""
        return BudgetLine(
            label=label,
            rate=rate,
            volume=self.volume,
            expected_failures=self.expected_failures(rate),
            budget_failures=self.budget_failures,
        )

    def scaled_to(self, volume: int) -> ErrorBudget:
        """The same objective over a different volume.

        Used to assess a bucket against the share of the queue that bucket
        actually represents. Assessing the short bucket's rate against the
        whole queue's volume is the arithmetic error that makes a partial
        launch look impossible.
        """
        return ErrorBudget(self.objective, volume, self.severity)
