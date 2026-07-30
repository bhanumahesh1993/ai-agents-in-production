"""Hard caps per run. Not warnings, not dashboards.

Four limits catch four distinct failures. Turns catch a loop. Spend catches
a tool that returns 40,000 tokens of JSON forty times. Wall clock catches a
run that is stuck rather than looping. Write count catches the fan-out case
a turn limit misses, where one turn issues twelve tool calls.

A fifth is worth adding for any tool that can address more than one object:
a distinct-resource cap, so a refund agent that touches its fortieth
distinct order in one run stops and asks.

Three of the five already exist. :class:`northstar_policy.BudgetGuard`
tracks cents, turns, and wall clock and raises rather than warning, so this
module composes it rather than restating it, and adds the two that count
*effects* rather than consumption.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from northstar_contracts import Money, RunState, ToolCall, ToolSpec
from northstar_policy import BudgetExceeded
from northstar_policy import BudgetGuard as CoreBudgetGuard

__all__ = ["BudgetExceeded", "BudgetGuard"]


class BudgetGuard:
    """Turns, cents, wall clock, writes, and distinct resources.

    Args:
        max_cents: Model spend allowed. ``None`` disables the limit.
        max_turns: Loop iterations allowed.
        max_seconds: Elapsed active time allowed.
        max_writes: Successful mutating calls allowed in one run.
        max_resources: Distinct resources one run may touch.
        run_id: Included in the raised error, so an alert names the run.
        clock: Injectable monotonic clock, so tests do not sleep.

    Example:
        >>> guard = BudgetGuard(max_writes=1)
        >>> guard.record_write("NR-2026-0042110")
        1
        >>> guard.record_write("NR-2026-0042110")
        Traceback (most recent call last):
        northstar_policy.budget.BudgetExceeded: writes budget exceeded: ...
    """

    def __init__(
        self,
        max_cents: Money | None = 200,
        max_turns: int | None = 12,
        max_seconds: float | None = 120.0,
        max_writes: int | None = 3,
        max_resources: int | None = 5,
        *,
        run_id: str | None = None,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self.core = CoreBudgetGuard(
            max_cents=max_cents,
            max_turns=max_turns,
            max_wall_seconds=max_seconds,
            run_id=run_id,
            clock=clock,
        )
        self.max_writes = max_writes
        self.max_resources = max_resources
        self.run_id = run_id
        self.writes = 0
        self.resources: set[str] = set()

    # --------------------------------------------------------------- limits

    def check(self, state: RunState) -> None:
        """Raise, never warn. Callers do not catch this.

        Reads the run's own counters rather than trusting the guard's,
        because a resumed run rebuilds the guard and would otherwise get a
        second full budget — the bug you get for free if you forget.

        Raises:
            BudgetExceeded: On any of the five limits.
        """
        self.core.turns = state.step
        self.core.spent_cents = state.budget_spent_cents
        self.core.check()
        if self.max_writes is not None and self.writes > self.max_writes:
            raise BudgetExceeded(
                "writes",  # type: ignore[arg-type]
                self.max_writes,
                self.writes,
                self.run_id,
            )
        if (
            self.max_resources is not None
            and len(self.resources) > self.max_resources
        ):
            raise BudgetExceeded(
                "resources",  # type: ignore[arg-type]
                self.max_resources,
                len(self.resources),
                self.run_id,
            )

    def record_write(self, resource: str = "") -> int:
        """Count one committed write, then check the limits.

        Called after the effect lands, not before, so the count is a fact
        about the world rather than about the agent's intentions.

        Returns:
            The number of writes so far.

        Raises:
            BudgetExceeded: When the write or distinct-resource cap breaks.
        """
        self.writes += 1
        if resource:
            self.resources.add(resource)
        if self.max_writes is not None and self.writes > self.max_writes:
            raise BudgetExceeded(
                "writes",  # type: ignore[arg-type]
                self.max_writes,
                self.writes,
                self.run_id,
            )
        if (
            self.max_resources is not None
            and len(self.resources) > self.max_resources
        ):
            raise BudgetExceeded(
                "resources",  # type: ignore[arg-type]
                self.max_resources,
                len(self.resources),
                self.run_id,
            )
        return self.writes

    def reserve(self, call: ToolCall, spec: ToolSpec | None) -> None:
        """Stop *before* an expensive write rather than after it.

        Knowing you are over budget once the money is gone is accounting,
        not control. :meth:`record_write` counts what landed;
        this refuses what would not fit.

        Raises:
            BudgetExceeded: When this write would break a cap.
        """
        if spec is None or not spec.writes:
            return
        if self.max_writes is not None and self.writes + 1 > self.max_writes:
            raise BudgetExceeded(
                "writes",  # type: ignore[arg-type]
                self.max_writes,
                self.writes + 1,
                self.run_id,
            )
        resource = str(call.arguments.get("order_id", ""))
        if (
            self.max_resources is not None
            and resource
            and resource not in self.resources
            and len(self.resources) + 1 > self.max_resources
        ):
            raise BudgetExceeded(
                "resources",  # type: ignore[arg-type]
                self.max_resources,
                len(self.resources) + 1,
                self.run_id,
            )

    def observe(self, call: ToolCall, spec: ToolSpec | None) -> None:
        """Record a call's effect on the write and resource budgets."""
        if spec is None or not spec.writes:
            return
        self.record_write(str(call.arguments.get("order_id", "")))

    def snapshot(self) -> dict[str, Any]:
        """JSON-serialisable state, for span attributes and journals."""
        return {
            **self.core.snapshot(),
            "writes": self.writes,
            "max_writes": self.max_writes,
            "distinct_resources": sorted(self.resources),
            "max_resources": self.max_resources,
        }
