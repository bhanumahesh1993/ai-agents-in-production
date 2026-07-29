"""Per-run budgets: money, turns, and wall clock.

An agent loop has no natural stopping condition. The model decides when it
is done, and a model that has lost the thread will happily call the same
tool forty times, or reason in circles until something else kills it. The
budget is the something else.

Three limits, because agents fail three ways:

* **cents** — the run that is working, expensively, forever;
* **turns** — the run stuck in a loop, cheaply, forever;
* **wall clock** — the run blocked on a tool that will never answer.

Any one of them alone leaves a hole. Set all three.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any, Literal

from northstar_contracts import Money

__all__ = [
    "BudgetExceeded",
    "BudgetGuard",
    "BudgetKind",
    "TurnLimitExceeded",
]

BudgetKind = Literal["cents", "turns", "wall_clock"]


class BudgetExceeded(RuntimeError):
    """A run hit one of its limits.

    This is one of the few exceptions the agent loop deliberately lets
    escape. A tool failure is an observation the model can work with; a
    budget breach is not something the model gets a vote on.
    """

    def __init__(
        self,
        kind: BudgetKind,
        limit: float,
        used: float,
        run_id: str | None = None,
    ) -> None:
        self.kind = kind
        self.limit = limit
        self.used = used
        self.run_id = run_id
        where = f" in run {run_id}" if run_id else ""
        super().__init__(
            f"{kind} budget exceeded{where}: used {used:g}, limit {limit:g}"
        )


class TurnLimitExceeded(BudgetExceeded):
    """The run used its whole turn allowance without finishing.

    A subclass of :class:`BudgetExceeded` so that ``except BudgetExceeded``
    catches every kind of exhaustion, while callers that want to retry with
    a larger turn allowance can still tell the cases apart.
    """

    def __init__(
        self,
        limit: float,
        used: float,
        run_id: str | None = None,
    ) -> None:
        super().__init__("turns", limit, used, run_id)


class BudgetGuard:
    """Tracks spend, turns, and elapsed time for one run.

    Args:
        max_cents: Total model spend allowed. ``None`` disables the limit.
        max_turns: Loop iterations allowed. ``None`` disables the limit.
        max_wall_seconds: Elapsed time allowed. ``None`` disables the limit.
        run_id: Included in the raised error, so an alert names the run.
        clock: Injectable monotonic clock, so tests do not sleep.

    Example:
        >>> guard = BudgetGuard(max_cents=200, max_turns=12)
        >>> guard.charge(150)
        150
        >>> guard.remaining_cents
        50
    """

    def __init__(
        self,
        max_cents: Money | None = 200,
        max_turns: int | None = 12,
        max_wall_seconds: float | None = None,
        *,
        run_id: str | None = None,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self.max_cents = max_cents
        self.max_turns = max_turns
        self.max_wall_seconds = max_wall_seconds
        self.run_id = run_id
        self._clock: Callable[[], float] = clock or time.monotonic
        self.spent_cents: Money = 0
        self.turns = 0
        self.started_at: float = self._clock()

    def start(self) -> BudgetGuard:
        """(Re)start the wall clock. Returns ``self``."""
        self.started_at = self._clock()
        return self

    @property
    def elapsed_seconds(self) -> float:
        """Seconds since :meth:`start`."""
        return self._clock() - self.started_at

    @property
    def remaining_cents(self) -> Money | None:
        """Spend left, or ``None`` when there is no money limit."""
        if self.max_cents is None:
            return None
        return max(0, self.max_cents - self.spent_cents)

    @property
    def remaining_turns(self) -> int | None:
        """Turns left, or ``None`` when there is no turn limit."""
        if self.max_turns is None:
            return None
        return max(0, self.max_turns - self.turns)

    def charge(self, cents: Money) -> Money:
        """Record spend, then check the limits.

        The order matters: charge first, then raise. A run that goes over
        budget on its last model call really did spend that money, and the
        ledger has to say so even though the run is about to die.

        Returns:
            Total spent so far.

        Raises:
            BudgetExceeded: When the money or wall-clock limit is passed.
        """
        self.spent_cents += int(cents)
        self.check()
        return self.spent_cents

    def tick(self) -> int:
        """Count one loop turn, then check the limits.

        Raises:
            TurnLimitExceeded: When the turn allowance is used up.
            BudgetExceeded: When another limit is passed.
        """
        self.turns += 1
        self.check()
        return self.turns

    def check(self) -> None:
        """Raise if any limit has been passed. Cheap; call it often.

        Raises:
            TurnLimitExceeded: On the turn limit.
            BudgetExceeded: On the money or wall-clock limit.
        """
        if self.max_turns is not None and self.turns > self.max_turns:
            raise TurnLimitExceeded(self.max_turns, self.turns, self.run_id)
        if self.max_cents is not None and self.spent_cents > self.max_cents:
            raise BudgetExceeded(
                "cents", self.max_cents, self.spent_cents, self.run_id
            )
        if self.max_wall_seconds is not None:
            elapsed = self.elapsed_seconds
            if elapsed > self.max_wall_seconds:
                raise BudgetExceeded(
                    "wall_clock", self.max_wall_seconds, elapsed, self.run_id
                )

    def would_exceed(self, cents: Money) -> bool:
        """Whether charging ``cents`` would break the money budget.

        Use this to stop *before* an expensive call rather than after it.
        Knowing you are over budget once the money is gone is accounting,
        not control.
        """
        if self.max_cents is None:
            return False
        return self.spent_cents + int(cents) > self.max_cents

    def snapshot(self) -> dict[str, Any]:
        """JSON-serialisable state, for span attributes and journals."""
        return {
            "spent_cents": self.spent_cents,
            "turns": self.turns,
            "elapsed_seconds": round(self.elapsed_seconds, 6),
            "max_cents": self.max_cents,
            "max_turns": self.max_turns,
            "max_wall_seconds": self.max_wall_seconds,
        }

    def __repr__(self) -> str:
        return (
            f"BudgetGuard(spent_cents={self.spent_cents}, "
            f"turns={self.turns}, max_cents={self.max_cents}, "
            f"max_turns={self.max_turns})"
        )
