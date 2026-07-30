"""Two numbers from the same runs: the one a leaderboard publishes and the
one that should gate a release.

A public benchmark reports one attempt per task and averages. That is a
capability measurement, and no amount of care in reading it recovers the
information it never collected. Repeating each task ``k`` times and reporting
``pass^k`` beside ``pass@1`` collects it, and the gap between the two is the
size of what the single-attempt number discards.

Cost and latency are recorded on the same runs, because a model that scores
two points higher and takes four times as long may be unusable for a support
queue with an eighteen-second p95 target, and that is a decision input rather
than a footnote.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from adapter import AttemptResult, attempt
from northstar_evals import pass_k as _pass_k
from northstar_runtime import ModelProvider
from task import BenchmarkTask, dual_control, solo

__all__ = [
    "SEED",
    "TaskReport",
    "breakdown",
    "compare",
    "percentile",
    "run_repeated",
    "run_suite",
    "summarise",
]

#: Fixed in configuration rather than chosen after seeing the results. A seed
#: picked because it made the new version look good is p-hacking with extra
#: steps.
SEED = 1729


@dataclass(frozen=True)
class TaskReport:
    """Every attempt at one task, in order."""

    task_id: str
    attempts: tuple[AttemptResult, ...]
    dual_control: bool = False

    @property
    def results(self) -> list[bool]:
        """Per-attempt verdicts."""
        return [a.passed for a in self.attempts]

    @property
    def pass_1(self) -> float:
        """Fraction of attempts that passed."""
        if not self.attempts:
            return 0.0
        return sum(self.results) / len(self.attempts)

    @property
    def all_passed(self) -> bool:
        """Whether every attempt passed. The ``pass^k`` question per task."""
        return bool(self.attempts) and all(self.results)

    def pass_pow_k(self, k: int) -> float:
        """This task's ``pass^k``, estimated from the attempts it has."""
        if len(self.attempts) < k:
            return 0.0
        return _pass_k(self.results, k)


def percentile(values: list[float], p: float) -> float:
    """Nearest-rank percentile.

    Args:
        values: The sample. May be empty, in which case the answer is 0.0.
        p: Percentile in ``[0, 100]``.

    Raises:
        ValueError: If ``p`` is outside ``[0, 100]``.
    """
    if not 0.0 <= p <= 100.0:
        raise ValueError(f"percentile must be in [0, 100], got {p}")
    if not values:
        return 0.0
    ordered = sorted(values)
    rank = max(1, -(-int(p) * len(ordered) // 100))
    return ordered[min(rank, len(ordered)) - 1]


def run_repeated(
    task: BenchmarkTask,
    n: int = 5,
    seed: int = SEED,
    model: ModelProvider | None = None,
) -> TaskReport:
    """Attempt one task ``n`` times from ``n`` different seeds.

    Every attempt rebuilds the world from the task's fixtures, so the
    mutating ``issue_refund`` call cannot leak state into the next attempt,
    and the derived idempotency key means a retry inside an attempt returns
    the first receipt instead of paying twice.
    """
    attempts = tuple(
        attempt(task, model, seed + index)   # type: ignore[arg-type]
        for index in range(n)
    )
    return TaskReport(
        task_id=task.task_id,
        attempts=attempts,
        dual_control=task.is_dual_control,
    )


def run_suite(
    tasks: list[BenchmarkTask],
    k: int = 5,
    seed: int = SEED,
    model: ModelProvider | None = None,
) -> list[TaskReport]:
    """Run every task ``k`` times. One pass over the set, reused downstream."""
    return [run_repeated(t, n=k, seed=seed, model=model) for t in tasks]


def summarise(reports: list[TaskReport], k: int = 5) -> dict[str, float]:
    """The two headline numbers and the cost ceiling, from one set of runs.

    ``headline_pass_at_1`` is deliberately computed from the *first*
    attempt at each task and nothing else, because that is exactly what a
    leaderboard reporting one attempt per task would have published.
    """
    if not reports:
        return {
            "headline_pass_at_1": 0.0,
            "pass_pow_k": 0.0,
            "p95_cost_cents": 0.0,
        }
    first = [r.attempts[0].passed for r in reports if r.attempts]
    all_k = [r.all_passed for r in reports]
    costs = [
        float(a.cost_cents) for r in reports for a in r.attempts
    ]
    return {
        "headline_pass_at_1": sum(first) / len(first) if first else 0.0,
        "pass_pow_k": sum(all_k) / len(all_k),
        "p95_cost_cents": percentile(costs, 95),
    }


def compare(
    tasks: list[BenchmarkTask],
    model: ModelProvider | None = None,
    k: int = 5,
) -> dict[str, float]:
    """Run the set and return the leaderboard number beside the release one.

    Args:
        tasks: The task set.
        model: Provider to run every attempt against. ``None`` uses the
            per-attempt seeded flaky model, which is the offline default.
        k: Repetitions per task.
    """
    return summarise(run_suite(tasks, k=k, model=model), k=k)


def breakdown(reports: list[TaskReport], tasks: list[BenchmarkTask]) -> (
    dict[str, dict[str, float]]
):
    """Split the same runs by whether the customer had to act.

    This is the slice that explains the gap. Tasks the agent can complete
    alone are close to deterministic; tasks that require a person to do
    something are where a good run and a bad run diverge.
    """
    by_id = {r.task_id: r for r in reports}
    groups = {
        "solo": [by_id[t.task_id] for t in solo(tasks) if t.task_id in by_id],
        "dual_control": [
            by_id[t.task_id] for t in dual_control(tasks)
            if t.task_id in by_id
        ],
    }
    out: dict[str, dict[str, float]] = {}
    for label, group in groups.items():
        attempts = [a for r in group for a in r.attempts]
        out[label] = {
            "tasks": float(len(group)),
            "attempts": float(len(attempts)),
            "pass_1": (
                sum(a.passed for a in attempts) / len(attempts)
                if attempts else 0.0
            ),
            "pass_pow_k": (
                sum(r.all_passed for r in group) / len(group)
                if group else 0.0
            ),
            "p95_latency_ms": percentile(
                [a.latency_ms for a in attempts], 95
            ),
        }
    return out


def unsafe_successes(reports: list[TaskReport]) -> list[dict[str, Any]]:
    """Attempts that reached a forbidden action, counted separately.

    A task that failed safely and a task that succeeded through a forbidden
    shortcut are not the same event, and one aggregate number hides both.
    """
    flagged: list[dict[str, Any]] = []
    for report in reports:
        for a in report.attempts:
            hits = [r for r in a.reasons if "forbidden tool" in r]
            if hits:
                flagged.append(
                    {"task_id": a.task_id, "seed": a.seed, "reasons": hits}
                )
    return flagged
