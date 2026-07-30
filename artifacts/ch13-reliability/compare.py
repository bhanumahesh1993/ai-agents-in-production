"""Compare two agent versions without fooling yourself.

The naive comparison runs the old version on the task set, runs the new one,
and compares two percentages. That needs roughly 690 runs per arm to resolve
85% from 90%, and it throws away the one advantage you actually have: both
versions can face the same tasks with the same seeds and the same fixtures.

Pairing removes task difficulty from the noise. You stop caring that task 7
is hard for everyone and start caring only about the runs where the two
versions disagreed. Two statistics come out of that:

* **McNemar's exact test** over the discordant pairs, which is a coin-flip
  question and is answered here by :func:`metrics.mcnemar_exact`;
* **a bootstrap over tasks** of the per-task difference, which gives an
  effect size with an interval rather than a bare p value.

Report both. "p < 0.05" says the difference is unlikely to be zero; it says
nothing about whether the difference is worth the latency the change added.
"""

from __future__ import annotations

from dataclasses import dataclass

import harness
import metrics
from harness import SuiteReport, Version
from tasks import Task

__all__ = ["PairedComparison", "compare_versions"]


@dataclass(frozen=True)
class PairedComparison:
    """What a paired two-version comparison found.

    Attributes:
        baseline: The suite report for the incumbent.
        candidate: The suite report for the challenger, measured on the
            same tasks at the same seeds.
        candidate_wins: Runs the candidate passed and the baseline failed.
        baseline_wins: Runs the baseline passed and the candidate failed.
        concordant: Runs both versions agreed on. Carried no information
            about which version is better, and excluded from the test.
        p_value: Two-sided exact McNemar p value over the discordant pairs.
        delta: Candidate pass@1 minus baseline pass@1, over the suite.
        delta_interval: Bootstrap interval over *tasks* for the mean
            per-task difference. Wider than a pooled interval, and honest.
    """

    baseline: SuiteReport
    candidate: SuiteReport
    candidate_wins: int
    baseline_wins: int
    concordant: int
    p_value: float
    delta: float
    delta_interval: tuple[float, float]

    @property
    def discordant(self) -> int:
        """Pairs the two versions disagreed on: the whole sample size."""
        return self.candidate_wins + self.baseline_wins

    @property
    def significant(self) -> bool:
        """Whether the disagreement is unlikely under a fair coin."""
        return self.p_value < 0.05

    def summary(self) -> str:
        """One line, the shape a release note should carry."""
        low, high = self.delta_interval
        return (
            f"{self.candidate.version.name} vs {self.baseline.version.name}: "
            f"delta {self.delta:+.3f} [{low:+.3f}, {high:+.3f}], "
            f"{self.candidate_wins}-{self.baseline_wins} on "
            f"{self.discordant} discordant pair(s), p={self.p_value:.3f}"
        )


def compare_versions(
    tasks: tuple[Task, ...],
    n: int,
    seed: int,
    baseline: Version = harness.BASELINE,
    candidate: Version = harness.CANDIDATE,
    k: int = 4,
) -> PairedComparison:
    """Measure two versions on the same tasks and seeds, and pair the runs.

    Args:
        tasks: The task set. Both versions see all of it.
        n: Repetitions per task per version.
        seed: Base seed, shared by both arms. This is what makes run ``i``
            of task ``t`` the *same* draw for both versions, which is what
            makes the comparison paired rather than merely simultaneous.
        baseline: The incumbent.
        candidate: The challenger.
        k: Which pass^k column the suite reports carry.

    Returns:
        A :class:`PairedComparison`.

    Raises:
        ValueError: If the two arms did not produce alignable run lists,
            which would mean the pairing is fictional.
    """
    base_report = harness.run_suite(
        tasks, n=n, seed=seed, k=k, version=baseline
    )
    cand_report = harness.run_suite(
        tasks, n=n, seed=seed, k=k, version=candidate
    )

    candidate_wins = 0
    baseline_wins = 0
    concordant = 0
    per_task_delta: list[float] = []

    for before, after in zip(
        base_report.reports, cand_report.reports, strict=True
    ):
        if before.task != after.task or len(before.results) != len(
            after.results
        ):
            raise ValueError(
                "cannot pair runs: the two arms measured different tasks or "
                f"different run counts ({before.task} vs {after.task})"
            )
        for was, now in zip(before.results, after.results, strict=True):
            if was == now:
                concordant += 1
            elif now:
                candidate_wins += 1
            else:
                baseline_wins += 1
        per_task_delta.append(after.pass_1 - before.pass_1)

    return PairedComparison(
        baseline=base_report,
        candidate=cand_report,
        candidate_wins=candidate_wins,
        baseline_wins=baseline_wins,
        concordant=concordant,
        p_value=metrics.mcnemar_exact(candidate_wins, baseline_wins),
        delta=cand_report.pass_1 - base_report.pass_1,
        delta_interval=metrics.bootstrap_over_tasks(per_task_delta),
    )
