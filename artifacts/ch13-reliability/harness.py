"""Run one task many times and report what the repetitions showed.

The statistical content is in ``metrics.py``. What lives here is the part that
is easier to get wrong: making the repetitions independent.
"""

from __future__ import annotations

from dataclasses import dataclass

from northstar_evals import ReliabilityReport, run_repeated as _run_repeated
from northstar_runtime import AgentLoop, FakeModel, FlakyModel

import metrics
from tasks import Task

# Failure rates for the wrapper. Chosen so the suite lands in the region the
# chapter is about -- high pass@1, visibly lower pass^k -- rather than at
# either extreme, where the arithmetic stops being interesting.
P_REPEAT = 0.06
P_STALL = 0.05
P_GIVEUP = 0.05


@dataclass(frozen=True)
class SuiteReport:
    """Per-task reports plus the suite-level figures over them."""

    reports: tuple[ReliabilityReport, ...]
    k: int

    @property
    def pass_1(self) -> float:
        """Runs that passed, over all runs in the suite."""
        total = sum(r.n for r in self.reports)
        return sum(r.successes for r in self.reports) / total if total else 0.0

    @property
    def pass_k(self) -> float:
        """Mean of the per-task pass^k figures.

        Averaged over tasks rather than pooled over runs on purpose. A task
        set is not homogeneous, and ``mean(p_i^k)`` is the quantity a
        measured suite actually estimates -- it is always at least
        ``mean(p_i)^k``, and the gap between them is how bimodal your task
        set is.
        """
        if not self.reports:
            return 0.0
        values = [r.pass_k_values.get(self.k, 0.0) for r in self.reports]
        return sum(values) / len(values)

    @property
    def bootstrap_interval(self) -> tuple[float, float]:
        """Interval over tasks, which is wider than the pooled one."""
        return metrics.bootstrap_over_tasks(
            [r.pass_1 for r in self.reports]
        )

    def for_task(self, task_id: str) -> ReliabilityReport | None:
        """One task's report, by id."""
        return next((r for r in self.reports if r.task == task_id), None)


def grade_once(task: Task, seed: int) -> bool:
    """Run ``task`` once against a fresh world and grade the world.

    Returns:
        Whether the run left the world in the expected state. An exception
        inside the loop is a failure, not an error, because a run that
        crashes did not do the task either.
    """
    world = task.build_world()               # fresh fixtures per run
    model = FlakyModel(
        FakeModel(default=list(task.script), strict=False),
        seed=seed,
        p_repeat=P_REPEAT,
        p_stall=P_STALL,
        p_giveup=P_GIVEUP,
    )
    loop = AgentLoop(model, task.tools(world), max_turns=12)
    try:
        state = loop.run(task.goal, run_id=f"run_{task.id}_{seed}")
    except Exception:                        # noqa: BLE001
        return False
    return task.expected().grade(state, world).passed


def run_repeated(task: Task, n: int, seed: int) -> ReliabilityReport:
    """Measure one task ``n`` times.

    ``task.build_world()`` happens inside :func:`grade_once`, once per
    repetition. That is the load-bearing line of this file. Reuse one world
    across repetitions and run 2 starts with run 1's refund already in the
    ledger, so a grader asserting "exactly one refund of 3,250 cents" fails
    every run after the first and the harness reports a 1/n success rate for
    an agent that is behaving correctly.

    Args:
        task: What to measure.
        n: Repetitions.
        seed: Base seed. Each repetition derives its own from this, so the
            whole report is reproducible.

    Returns:
        A :class:`northstar_evals.ReliabilityReport`.
    """
    return _run_repeated(
        lambda run_seed: grade_once(task, run_seed),
        n=n,
        seed=seed,
        name=task.id,
        k_values=(1, 2, 4, 8),
    )


def run_suite(
    tasks: tuple[Task, ...], n: int, seed: int, k: int = 4
) -> SuiteReport:
    """Measure every task in ``tasks`` and collect the reports."""
    return SuiteReport(
        reports=tuple(run_repeated(t, n=n, seed=seed) for t in tasks),
        k=k,
    )


def run_shared_world_suite(
    tasks: tuple[Task, ...], n: int, seed: int, k: int = 4
) -> SuiteReport:
    """The broken harness, kept so the demo can show what it does.

    One world for every repetition of a task. Nothing about the agent
    changes; the measurement collapses anyway.
    """

    def broken(task: Task) -> ReliabilityReport:
        world = task.build_world()           # hoisted out of the loop: the bug

        def once(run_seed: int) -> bool:
            model = FlakyModel(
                FakeModel(default=list(task.script), strict=False),
                seed=run_seed,
                p_repeat=P_REPEAT,
                p_stall=P_STALL,
                p_giveup=P_GIVEUP,
            )
            loop = AgentLoop(model, task.tools(world), max_turns=12)
            try:
                state = loop.run(
                    task.goal, run_id=f"shared_{task.id}_{run_seed}"
                )
            except Exception:                # noqa: BLE001
                return False
            return task.expected().grade(state, world).passed

        return _run_repeated(
            once, n=n, seed=seed, name=task.id, k_values=(1, 2, 4, 8)
        )

    return SuiteReport(reports=tuple(broken(t) for t in tasks), k=k)
