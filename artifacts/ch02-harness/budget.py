"""The three exits that belong to code, plus the no-progress detector.

A production loop has four independent exits and only one of them, the
absence of tool calls, belongs to the model. The other three live here,
where nothing the model emits can reach them.

This is the chapter's guard, not the repository's.
``northstar_policy.BudgetGuard`` enforces cents, turns, and wall clock for
every other artifact; this one adds the two refinements Chapter 2 argues
for:

* **active seconds, not elapsed seconds.** A run suspended for two hours
  waiting on a human should not fail a deadline computed before it was
  suspended, so suspension intervals recorded in the journal are
  subtracted.
* **a no-progress detector.** A model calling the same tool with the same
  arguments over and over burns all three budgets while accomplishing
  nothing, and none of the three notices.

Every limit raises. Returning a partial result with status ``succeeded`` is
the failure Chapter 1 is about, moved one layer out.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

from northstar_contracts import Money, RunState, canonical_json
from northstar_policy import BudgetExceeded as PolicyBudgetExceeded
from northstar_policy import BudgetKind

__all__ = ["BudgetExceeded", "BudgetGuard"]


class BudgetExceeded(PolicyBudgetExceeded):
    """A limit was reached. ``detail`` names which one, and its value.

    Subclasses ``northstar_policy.BudgetExceeded`` so code written against
    the repository's guard still catches the chapter's four-limit one. The
    parent reports ``(kind, limit, used)``; this guard reports the limit as
    one readable string, so the message is rebuilt rather than derived.
    """

    def __init__(self, detail: str, kind: BudgetKind = "turns") -> None:
        super().__init__(kind, 0.0, 0.0)
        self.detail = detail
        self.args = (f"budget exceeded: {detail}",)


class BudgetGuard:
    """Turn, money, deadline, and no-progress limits for one run.

    Args:
        max_turns: Model calls allowed. Northstar runs with 12, which is
            the number the autonomy worksheet produced rather than a
            default someone inherited.
        budget_cents: Model spend allowed, in integer cents.
        deadline_s: Seconds of *active* work allowed. Time spent suspended
            for an approval does not count.
        max_repeats: How many times the same call with the same arguments
            may repeat before the run is treated as stuck.
        journal: Consulted for suspension intervals. Without one,
            :meth:`active_seconds` degrades to elapsed seconds, which is
            the behaviour the chapter warns about.
        clock: Injectable monotonic clock, so tests do not sleep.

    Example:
        >>> guard = BudgetGuard(max_turns=2).start()
        >>> guard.check(RunState(run_id="r", step=1))       # room left
        >>> try:
        ...     guard.check(RunState(run_id="r", step=2))
        ... except BudgetExceeded as exc:
        ...     print(exc)
        budget exceeded: max_turns=2
    """

    def __init__(
        self,
        max_turns: int = 12,
        budget_cents: Money = 200,
        deadline_s: float = 900.0,
        max_repeats: int = 3,
        *,
        journal: Any | None = None,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self.max_turns = max_turns
        self.budget_cents = budget_cents
        self.deadline_s = deadline_s
        self.max_repeats = max_repeats
        self.journal = journal
        self._clock: Callable[[], float] = clock or time.monotonic
        self.started_at: float = self._clock()

    def start(self) -> BudgetGuard:
        """(Re)start the active-time clock. Returns ``self``."""
        self.started_at = self._clock()
        return self

    def active_seconds(self, state: RunState) -> float:
        """Seconds this run spent working, suspensions excluded.

        Not "now minus start time". A run that sat for two hours waiting
        for someone to approve a 12,000-cent refund did not spend two hours
        working, and a deadline that says otherwise fails every approval
        that arrives after lunch.
        """
        elapsed = self._clock() - self.started_at
        suspended = 0.0
        if self.journal is not None:
            suspended = float(self.journal.suspended_seconds(state.run_id))
        return max(0.0, elapsed - suspended)

    def repeated_calls(self, state: RunState) -> int:
        """Length of the trailing run of identical calls in the history.

        Identical means the same tool *and* the same arguments. A model
        reading three different orders is working; a model reading the same
        order four times is not, and no token or turn budget can tell those
        apart before the money is gone.
        """
        signatures = [
            (call.name, canonical_json(call.arguments))
            for message in state.messages
            for call in message.tool_calls
        ]
        if not signatures:
            return 0
        last = signatures[-1]
        count = 0
        for signature in reversed(signatures):
            if signature != last:
                break
            count += 1
        return count

    def check(self, state: RunState) -> None:
        """Raise if any limit has been reached. Called before the model.

        Checked *before* the model call, not after it: a budget checked
        afterwards has already been exceeded, and the run has already paid
        for the turn that broke it.

        Raises:
            BudgetExceeded: Naming the limit that stopped the run.
        """
        if state.step >= self.max_turns:
            raise BudgetExceeded(f"max_turns={self.max_turns}")
        if state.budget_spent_cents >= self.budget_cents:
            raise BudgetExceeded(
                f"budget_cents={self.budget_cents}", kind="cents"
            )
        if self.active_seconds(state) >= self.deadline_s:
            raise BudgetExceeded(
                f"deadline_s={self.deadline_s}", kind="wall_clock"
            )
        if self.repeated_calls(state) >= self.max_repeats:
            raise BudgetExceeded("no progress: repeated call")

    def snapshot(self, state: RunState) -> dict[str, Any]:
        """The four limits and their current usage, for printing."""
        return {
            "turns": f"{state.step}/{self.max_turns}",
            "cents": f"{state.budget_spent_cents}/{self.budget_cents}",
            "active_s": round(self.active_seconds(state), 3),
            "repeats": f"{self.repeated_calls(state)}/{self.max_repeats}",
        }
