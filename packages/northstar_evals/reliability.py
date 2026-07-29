"""Repeated-run reliability: pass@1, pass^k, and a confidence interval.

A single green run tells you the agent *can* do the task. It tells you
nothing about whether it *will*. Agents are stochastic, and the number that
matters in production is not "did it work" but "how often, and how badly
does that decay as the task gets longer".

Two statistics, and the gap between them is the whole point:

* **pass@1** — the chance one run succeeds. This is the number everyone
  quotes.
* **pass^k** — the chance *k* independent runs all succeed. This is the
  number your customers experience, because they do not each get their own
  best-of-k. At 90% per run, eight runs all succeeding is 43%.

Both are reported with a Wilson interval, because a proportion measured
over twenty runs has an honest uncertainty of roughly plus or minus twenty
points and reporting it as a bare percentage is how a reliability review
reaches the wrong conclusion politely.
"""

from __future__ import annotations

import inspect
import math
import statistics
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

__all__ = [
    "ReliabilityReport",
    "pass_at_k",
    "pass_k",
    "run_repeated",
    "wilson_interval",
]


def pass_k(results: Sequence[bool], k: int) -> float:
    """Estimate pass^k: the probability that ``k`` runs all succeed.

    Uses the unbiased combinatorial estimator ``C(c, k) / C(n, k)``, where
    ``c`` is the number of successes and ``n`` the number of runs — the
    probability that ``k`` runs drawn from the observed sample are all
    successes. It is the mirror image of the usual pass@k estimator, and it
    is the one that matters when every run has to work.

    Args:
        results: One boolean per run.
        k: How many consecutive successes are required.

    Returns:
        A probability in ``[0.0, 1.0]``.

    Raises:
        ValueError: If ``k`` is below 1 or exceeds the number of runs. The
            second case is not a rounding problem: you cannot estimate
            pass^8 from five runs, and returning a number anyway is worse
            than refusing.

    Example:
        >>> pass_k([True] * 9 + [False], 1)
        0.9
        >>> round(pass_k([True] * 9 + [False], 3), 4)
        0.7
    """
    n = len(results)
    if k < 1:
        raise ValueError(f"k must be at least 1, got {k}")
    if n == 0:
        raise ValueError("cannot estimate pass^k from zero runs")
    if k > n:
        raise ValueError(
            f"cannot estimate pass^{k} from {n} run(s); run at least {k}"
        )
    successes = sum(1 for r in results if r)
    if successes < k:
        return 0.0
    return math.comb(successes, k) / math.comb(n, k)


def pass_at_k(results: Sequence[bool], k: int) -> float:
    """Estimate pass@k: the probability that *at least one* of ``k`` passes.

    Included for contrast, not for gating. pass@k rises towards 1.0 as you
    allow more attempts, which flatters any agent you are willing to retry.
    It is the right measure when a human picks the best of several drafts,
    and the wrong one when the agent moves money on the first attempt.
    """
    n = len(results)
    if k < 1:
        raise ValueError(f"k must be at least 1, got {k}")
    if k > n:
        raise ValueError(f"cannot estimate pass@{k} from {n} run(s)")
    failures = n - sum(1 for r in results if r)
    if failures < k:
        return 1.0
    return 1.0 - math.comb(failures, k) / math.comb(n, k)


def wilson_interval(
    successes: int,
    n: int,
    confidence: float = 0.95,
) -> tuple[float, float]:
    """A Wilson score interval for a proportion.

    Wilson rather than the textbook normal approximation because agent
    evaluations run small and land near the edges, which is exactly where
    the normal approximation produces intervals extending past 1.0 and
    zero-width intervals at 0 successes.

    Args:
        successes: Number of successful runs.
        n: Total runs.
        confidence: Two-sided confidence level.

    Returns:
        ``(low, high)``, both clamped to ``[0.0, 1.0]``.

    Example:
        >>> low, high = wilson_interval(19, 20)
        >>> round(low, 3), round(high, 3)
        (0.764, 0.991)
    """
    if n <= 0:
        return (0.0, 1.0)
    if not 0.0 < confidence < 1.0:
        raise ValueError(f"confidence must be in (0, 1), got {confidence}")

    z = statistics.NormalDist().inv_cdf(1.0 - (1.0 - confidence) / 2.0)
    phat = successes / n
    denominator = 1.0 + z * z / n
    centre = (phat + z * z / (2 * n)) / denominator
    margin = (
        z
        * math.sqrt(phat * (1 - phat) / n + z * z / (4 * n * n))
        / denominator
    )
    return (max(0.0, centre - margin), min(1.0, centre + margin))


@dataclass(frozen=True)
class ReliabilityReport:
    """What repeated runs of one task actually showed.

    Attributes:
        task: Name of the task measured.
        n: Runs executed.
        successes: Runs that passed.
        results: The per-run booleans, in order. Kept so a reviewer can
            see whether the failures clustered — three in a row is a
            different problem from three scattered.
        seed: The seed the runs were derived from, so this is repeatable.
        pass_k_values: ``k`` to estimated pass^k.
        confidence: Confidence level used for the interval.
        durations: Per-run wall-clock seconds.
        errors: Exception strings for runs that blew up rather than failed.
    """

    task: str
    n: int
    successes: int
    results: list[bool] = field(default_factory=list)
    seed: int = 0
    pass_k_values: dict[int, float] = field(default_factory=dict)
    confidence: float = 0.95
    durations: list[float] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def pass_1(self) -> float:
        """The headline number: how often a single run succeeds."""
        return self.successes / self.n if self.n else 0.0

    @property
    def interval(self) -> tuple[float, float]:
        """Wilson interval around :attr:`pass_1`."""
        return wilson_interval(self.successes, self.n, self.confidence)

    @property
    def mean_duration(self) -> float:
        """Mean wall-clock seconds per run."""
        return statistics.fmean(self.durations) if self.durations else 0.0

    @property
    def p95_duration(self) -> float:
        """Ninety-fifth percentile of run duration, by nearest rank."""
        if not self.durations:
            return 0.0
        ordered = sorted(self.durations)
        index = max(0, math.ceil(0.95 * len(ordered)) - 1)
        return ordered[index]

    def to_dict(self) -> dict[str, Any]:
        """JSON-serialisable form, suitable for a CI artifact."""
        low, high = self.interval
        return {
            "task": self.task,
            "n": self.n,
            "successes": self.successes,
            "pass_1": round(self.pass_1, 6),
            "pass_k": {
                str(k): round(v, 6)
                for k, v in sorted(self.pass_k_values.items())
            },
            "confidence": self.confidence,
            "ci_low": round(low, 6),
            "ci_high": round(high, 6),
            "seed": self.seed,
            "mean_duration_seconds": round(self.mean_duration, 6),
            "p95_duration_seconds": round(self.p95_duration, 6),
            "errors": list(self.errors),
            "results": list(self.results),
        }

    def summary(self) -> str:
        """One human-readable line, the shape a CI log should print."""
        low, high = self.interval
        ks = " ".join(
            f"pass^{k}={v:.2f}"
            for k, v in sorted(self.pass_k_values.items())
        )
        return (
            f"{self.task}: {self.successes}/{self.n} "
            f"pass@1={self.pass_1:.2f} "
            f"[{low:.2f}, {high:.2f}] {ks}"
        )


def run_repeated(
    task: Callable[..., Any],
    n: int = 10,
    seed: int = 0,
    *,
    name: str = "",
    k_values: Sequence[int] = (1, 2, 4, 8),
    confidence: float = 0.95,
    stop_on_error: bool = False,
) -> ReliabilityReport:
    """Run one task ``n`` times and report how reliable it was.

    Args:
        task: Callable returning a truthy verdict. It may take no
            arguments, or one argument, in which case it receives a
            per-run seed derived from ``seed`` and the run index — so
            every run is different from its siblings and identical across
            invocations of this function. A ``GradeResult`` is accepted as
            a return value and read through its ``passed`` field.
        n: How many runs.
        seed: Base seed.
        name: Task name for the report.
        k_values: Which pass^k figures to compute. Values above ``n`` are
            skipped rather than raising, so one shared list of k values can
            be used across suites of different sizes.
        confidence: Confidence level for the interval.
        stop_on_error: Re-raise the first exception instead of counting it
            as a failure. Useful when you are debugging the harness rather
            than measuring the agent.

    Returns:
        A :class:`ReliabilityReport`.

    Example:
        >>> report = run_repeated(lambda s: s % 4 != 0, n=8, seed=1)
        >>> report.n
        8
    """
    if n < 1:
        raise ValueError(f"n must be at least 1, got {n}")

    takes_seed = _accepts_argument(task)
    results: list[bool] = []
    durations: list[float] = []
    errors: list[str] = []

    for index in range(n):
        run_seed = seed * 1_000_003 + index
        started = time.perf_counter()
        try:
            outcome = task(run_seed) if takes_seed else task()
        except Exception as exc:  # noqa: BLE001 - a crash is a failure
            if stop_on_error:
                raise
            errors.append(f"run {index}: {type(exc).__name__}: {exc}")
            results.append(False)
        else:
            results.append(bool(getattr(outcome, "passed", outcome)))
        durations.append(time.perf_counter() - started)

    return ReliabilityReport(
        task=name or getattr(task, "__name__", "task"),
        n=n,
        successes=sum(results),
        results=results,
        seed=seed,
        pass_k_values={
            k: pass_k(results, k) for k in sorted(k_values) if 1 <= k <= n
        },
        confidence=confidence,
        durations=durations,
        errors=errors,
    )


def _accepts_argument(task: Callable[..., Any]) -> bool:
    """Whether ``task`` takes a positional argument (its seed)."""
    try:
        signature = inspect.signature(task)
    except (TypeError, ValueError):  # pragma: no cover - builtins
        return False
    return any(
        parameter.kind
        in (
            inspect.Parameter.POSITIONAL_ONLY,
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
            inspect.Parameter.VAR_POSITIONAL,
        )
        for parameter in signature.parameters.values()
    )
