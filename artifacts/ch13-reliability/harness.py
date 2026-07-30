"""Run one task many times and report what the repetitions showed.

The statistical content is in ``metrics.py``. What lives here is the part
that is easier to get wrong: making the repetitions independent, and giving
two agent versions the same tasks and the same seeds so a comparison can be
paired.
"""

from __future__ import annotations

from dataclasses import dataclass

import metrics
from northstar_contracts import short_hash
from northstar_evals import ReliabilityReport
from northstar_evals import run_repeated as _run_repeated
from northstar_runtime import AgentLoop, FakeModel, FlakyModel, ToolRegistry
from tasks import Task


@dataclass(frozen=True)
class Version:
    """One agent version under measurement.

    A "version" here is a flakiness profile over the same plans, which is
    the cheapest honest stand-in for a prompt or model change: it moves how
    often the agent wastes a turn, repeats itself, or stops early, and it
    changes nothing about what the tasks are or how they are graded. That
    is the only shape of change a paired comparison can actually attribute.

    Args:
        name: Printed in reports and used to label a comparison column.
        p_repeat: Probability per turn of reissuing the previous call. On a
            read this costs a turn; on ``issue_refund`` it costs money,
            because the repeat lands in a new step and so derives a new
            idempotency key.
        p_stall: Probability per turn of a wasted read.
        p_giveup: Probability per turn of declaring success early.
    """

    name: str
    p_repeat: float
    p_stall: float
    p_giveup: float

    @property
    def p_interference(self) -> float:
        """Chance that a turn is not the turn the plan asked for."""
        return self.p_repeat + self.p_stall + self.p_giveup


#: The version the report leads with. Chosen so the suite lands in the region
#: the chapter is about -- high pass@1, visibly lower pass^k -- rather than at
#: either extreme, where the arithmetic stops being interesting.
BASELINE = Version("v3 (shipped)", p_repeat=0.026, p_stall=0.098,
                   p_giveup=0.007)

#: A candidate that stalls and gives up less often. This is the change
#: ``--compare`` measures, and the point of the exercise is that the paired
#: test can resolve it on far fewer runs than an unpaired one could.
CANDIDATE = Version("v4 (candidate)", p_repeat=0.022, p_stall=0.085,
                    p_giveup=0.006)


@dataclass(frozen=True)
class SuiteReport:
    """Per-task reports plus the suite-level figures over them."""

    reports: tuple[ReliabilityReport, ...]
    k: int
    version: Version = BASELINE

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


def build_loop(task: Task, world: object, model: object) -> AgentLoop:
    """Wire one run: a keyed registry, this task's tools, its turn ceiling.

    ``inject_idempotency_key=True`` is the load-bearing argument. Chapter 1
    left it off so the double refund could be demonstrated; by Chapter 13
    the repair has shipped, so a timeout that fires *after* the write lands
    is retried under the same derived key and collapses to one refund. That
    is what makes ``refund-after-timeout`` a recovery measurement rather
    than a guaranteed failure.
    """
    registry = ToolRegistry(inject_idempotency_key=True)
    registry.register_all(task.tools(world))          # type: ignore[arg-type]
    return AgentLoop(model, registry, max_turns=task.max_turns)  # type: ignore[arg-type]


def model_seed(task: Task, seed: int) -> int:
    """Fold the task id into the per-run seed.

    Without this every task of the same length draws the same interference
    from the same run seed, and the report prints six identical rows for
    six different tasks. Folding the id in decorrelates the tasks while
    keeping the whole thing reproducible, and -- because it depends on the
    task and the run and not on the version -- it keeps a two-version
    comparison genuinely paired.
    """
    return (seed + int(short_hash(task.id, 8), 16)) % (2**31)


def build_model(task: Task, seed: int, version: Version) -> FlakyModel:
    """The seeded wrapper over this task's plan.

    The script is the plan object repeated once per allowed turn, so the
    model can be interrupted and still know what to do next. ``strict`` is
    off because a run that burns its ceiling should end on the turn guard,
    which is a measurable failure, rather than on a scripting error.
    """
    base = FakeModel(default=[task.plan] * task.max_turns, strict=False)
    return FlakyModel(
        base,
        seed=model_seed(task, seed),
        p_repeat=version.p_repeat,
        p_stall=version.p_stall,
        p_giveup=version.p_giveup,
    )


def grade_once(task: Task, seed: int, version: Version = BASELINE) -> bool:
    """Run ``task`` once against a fresh world and grade the world.

    Returns:
        Whether the run left the world in the expected state. An exception
        inside the loop -- a turn ceiling, most often -- is a failure and
        not an error, because a run that died did not do the task either.
    """
    world = task.build_world()               # fresh fixtures per run
    loop = build_loop(task, world, build_model(task, seed, version))
    try:
        state = loop.run(task.goal, run_id=f"run_{task.id}_{seed}")
    except Exception:                        # noqa: BLE001
        return False
    return task.expected().grade(state, world).passed


def run_repeated(
    task: Task,
    n: int,
    seed: int,
    version: Version = BASELINE,
) -> ReliabilityReport:
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
            whole report is reproducible, and two versions measured at the
            same base seed face the same per-run draws.
        version: Which flakiness profile to run under.

    Returns:
        A :class:`northstar_evals.ReliabilityReport`.
    """
    return _run_repeated(
        lambda run_seed: grade_once(task, run_seed, version),
        n=n,
        seed=seed,
        name=task.id,
        k_values=(1, 2, 4, 8),
    )


def run_suite(
    tasks: tuple[Task, ...],
    n: int,
    seed: int,
    k: int = 4,
    version: Version = BASELINE,
) -> SuiteReport:
    """Measure every task in ``tasks`` and collect the reports."""
    return SuiteReport(
        reports=tuple(
            run_repeated(t, n=n, seed=seed, version=version) for t in tasks
        ),
        k=k,
        version=version,
    )


def run_shared_world_suite(
    tasks: tuple[Task, ...],
    n: int,
    seed: int,
    k: int = 4,
) -> SuiteReport:
    """The broken harness, kept so the demo can show what it does.

    One world for every repetition of a task. Nothing about the agent
    changes; the measurement collapses anyway, and it collapses in a shape
    that reads exactly like an agent regression.
    """

    def broken(task: Task) -> ReliabilityReport:
        world = task.build_world()           # hoisted out of the loop: the bug

        def once(run_seed: int) -> bool:
            loop = build_loop(
                task, world, build_model(task, run_seed, BASELINE)
            )
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
