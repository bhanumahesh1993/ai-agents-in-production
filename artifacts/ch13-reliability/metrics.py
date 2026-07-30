"""The two estimators Chapter 13 reports, under the names it prints.

Both delegate to ``northstar_evals``. That is deliberate and it is the
repository's dependency rule doing its job: a second implementation of
``pass^k`` living in an artifact is a second thing to keep correct, and the
first time the two disagree a reader has no way to tell which one the book
meant. ``test_ch13.py`` asserts the two agree over a grid rather than
trusting the delegation.

The maths, since the point of the chapter is that you should be able to check
it:

``pass^k`` is the probability that all ``k`` of ``k`` attempts succeed. You do
not need to run the agent in blocks of ``k`` to estimate it. Take every
``k``-sized subset of the ``n`` runs you have and ask in what fraction of them
every run passed. With ``c`` successes out of ``n`` that is a ratio of binomial
coefficients, ``C(c, k) / C(n, k)``, which is an unbiased estimate of the
all-succeed probability.

``wilson`` is the Wilson score interval rather than the textbook normal
approximation, because agent evaluations run small and land near the ends of
the scale, which is exactly where the normal approximation produces intervals
extending past 1.0 or collapsing to zero width at 0 successes.
"""

from __future__ import annotations

import statistics
from collections.abc import Sequence

from northstar_evals import pass_at_k as _pass_at_k
from northstar_evals import pass_k as _pass_k
from northstar_evals import wilson_interval as _wilson_interval

__all__ = ["bootstrap_over_tasks", "mcnemar_exact", "pass_at_k", "pass_k",
           "wilson"]


def pass_k(results: Sequence[bool], k: int) -> float:
    """Fraction of k-subsets of these runs in which all k passed.

    Args:
        results: Per-run verdicts, in order.
        k: How many consecutive successes the operation requires.

    Returns:
        The estimated ``pass^k``. ``k == 1`` reduces to the plain success
        rate, which is why one function serves both columns of the report.

    Raises:
        ValueError: If ``k`` exceeds the number of runs. Reporting
            ``pass^8`` from five runs is not a conservative estimate, it is
            an undefined one.
    """
    return _pass_k(results, k)


def pass_at_k(results: Sequence[bool], k: int) -> float:
    """Probability that *at least one* of k attempts passed.

    Included so the two metrics sit next to each other in one file. This is
    the code-generation metric: it rises with ``k`` while ``pass_k`` falls,
    and it is only the honest number when a human discards the failures.
    """
    return _pass_at_k(results, k)


def wilson(hits: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """Wilson score interval for a proportion, parameterised by ``z``.

    Args:
        hits: Successful runs.
        n: Total runs.
        z: Standard normal deviate. The default 1.96 is the two-sided 95%
            value, which is what a launch review means by "95% confidence".

    Returns:
        ``(low, high)``, both clamped to ``[0.0, 1.0]``.
    """
    if n <= 0:
        return (0.0, 1.0)
    confidence = _confidence_for(z)
    return _wilson_interval(hits, n, confidence)


def _confidence_for(z: float) -> float:
    """Convert a z score to the two-sided confidence level it implies.

    ``northstar_evals.wilson_interval`` takes a confidence level because
    that is what a caller usually knows; the chapter's excerpt takes ``z``
    because that is what the formula uses. This is the one line that
    reconciles them, and it is exact rather than a lookup table.
    """
    if z <= 0:
        raise ValueError(f"z must be positive, got {z}")
    return 2.0 * statistics.NormalDist().cdf(z) - 1.0


def bootstrap_over_tasks(
    per_task: Sequence[float],
    *,
    resamples: int = 2000,
    seed: int = 0,
    confidence: float = 0.95,
) -> tuple[float, float]:
    """Percentile bootstrap over *tasks*, not over runs.

    Twenty repetitions of twelve tasks is 240 runs but twelve samples of the
    thing you want to generalise to, and the runs within a task are strongly
    correlated. A Wilson interval over all 240 as though they were
    independent understates the uncertainty. Resampling the tasks with
    replacement does not.

    Args:
        per_task: One statistic per task, for example each task's pass@1.
        resamples: Bootstrap replicates.
        seed: Fixed, so a report is reproducible.
        confidence: Two-sided level.

    Returns:
        ``(low, high)`` percentiles of the resampled means.
    """
    import random

    if not per_task:
        return (0.0, 0.0)
    rng = random.Random(seed)
    size = len(per_task)
    means: list[float] = []
    for _ in range(resamples):
        draw = [per_task[rng.randrange(size)] for _ in range(size)]
        means.append(statistics.fmean(draw))
    means.sort()
    tail = (1.0 - confidence) / 2.0
    low = means[max(0, int(tail * resamples) - 1)]
    high = means[min(resamples - 1, int((1.0 - tail) * resamples))]
    return (low, high)


def mcnemar_exact(wins_a: int, wins_b: int) -> float:
    """Exact two-sided McNemar p value from the discordant pairs only.

    The test that makes a paired comparison affordable. Concordant pairs —
    tasks both versions passed, or both failed — carry no information about
    which version is better, so they are excluded and only the disagreements
    are counted. Under the null the split is a fair coin.

    Args:
        wins_a: Tasks the new version passed and the old one failed.
        wins_b: Tasks the old version passed and the new one failed.

    Returns:
        Two-sided p value. Returns 1.0 when there are no discordant pairs,
        which is the correct answer: no evidence either way.
    """
    from math import comb

    n = wins_a + wins_b
    if n == 0:
        return 1.0
    smaller = min(wins_a, wins_b)
    tail = sum(comb(n, i) for i in range(smaller + 1)) / (2.0**n)
    return min(1.0, 2.0 * tail)
