"""Walking a candidate up the canary ladder on real readings.

The controller in ``canary.py`` takes SLO readings as data. This module is
where those readings come from: it runs the critical suite at each cohort
and counts what happened.

Two details are worth stating, because they are where a canary usually goes
wrong.

**A read-only cohort cannot be graded on state.** Nothing changed, so a
state grader has nothing to read. The read-only rung is therefore graded on
*decisions*: the candidate is shadowed alongside the incumbent and a run
counts as a success when the two would have done the same thing. That is
the only honest reading available before writes are enabled, and it is a
useful one.

**A write the ceiling refused is not a failure.** Bounding a cohort by
amount means tickets over the bound go to the incumbent. Counting those as
candidate failures would make every bounded stage look like a regression
and teach the team to widen the bound.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field

from canary import COHORTS, CanaryController, CanaryStage, FlagSet, SloReading
from deployment import CRITICAL, Scenario, run_once
from shadow import compare, shadow_run
from versions import V8, AgentVersion

__all__ = ["CohortObservation", "observe_cohort", "walk"]


@dataclass(frozen=True)
class CohortObservation:
    """One rung's readings, with what was routed away from the candidate."""

    stage: CanaryStage
    reading: SloReading
    deferred: int
    notes: list[str] = field(default_factory=list)

    def line(self) -> str:
        """One row for the rollout report."""
        return (
            f"{self.stage.describe():<32}"
            f"runs={self.reading.runs} "
            f"verified={self.reading.verified_success:.2f} "
            f"integrity={self.reading.action_integrity:.2f} "
            f"deferred={self.deferred}"
        )


def observe_cohort(
    version: AgentVersion,
    stage: CanaryStage,
    scenarios: Sequence[Scenario] = CRITICAL,
    *,
    incumbent: AgentVersion = V8,
    seeds: Sequence[int] = (0, 1, 2),
) -> CohortObservation:
    """Run the suite at one rung and count what actually happened."""
    runs = successes = attempted = correct = deferred = 0
    notes: list[str] = []

    for scenario in scenarios:
        if not stage.writes_enabled:
            candidate = shadow_run(version, scenario)
            baseline = shadow_run(incumbent, scenario)
            diff = compare(baseline, candidate)
            runs += 1
            successes += int(diff.identical)
            if not diff.identical:
                notes.extend(diff.report())
            continue

        for seed in seeds:
            outcome = run_once(
                version,
                scenario,
                seed=seed,
                flags=FlagSet(),
                stage=stage,
                run_id=f"canary_{version.name}_{scenario.name}_{seed}",
            )
            if outcome.tools.blocked_by("ceiling"):
                deferred += 1
                notes.append(
                    f"{scenario.name}: over the {stage.ceiling_cents}c "
                    f"ceiling, deferred to {incumbent.name}"
                )
                continue
            runs += 1
            successes += int(outcome.passed)
            attempted += outcome.tools.mutations_attempted
            correct += outcome.tools.mutations_correct
            if not outcome.passed and outcome.grade.reasons:
                notes.append(f"{scenario.name}: {outcome.grade.reasons[0]}")

    return CohortObservation(
        stage=stage,
        reading=SloReading(
            cohort=stage.cohort,
            runs=runs,
            verified_successes=successes,
            mutations_attempted=attempted,
            mutations_correct=correct,
        ),
        deferred=deferred,
        notes=notes,
    )


def walk(
    version: AgentVersion,
    *,
    flags: FlagSet | None = None,
    scenarios: Sequence[Scenario] = CRITICAL,
) -> tuple[CanaryController, list[CohortObservation]]:
    """Take a candidate up the ladder until it completes or is contained."""
    controller = CanaryController(flags or FlagSet())
    seen: list[CohortObservation] = []

    for _ in COHORTS:
        observation = observe_cohort(version, controller.stage, scenarios)
        seen.append(observation)
        outcome = controller.observe(observation.reading)
        if outcome in ("contained", "held"):
            break
    return controller, seen
