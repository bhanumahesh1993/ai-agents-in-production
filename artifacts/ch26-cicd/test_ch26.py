"""The Chapter 26 release and containment properties, as assertions.

Every test asserts on something measured: a blocked release, a recorded
intent, a mutation that did or did not land.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import pytest  # noqa: E402
from canary import (  # noqa: E402
    COHORTS,
    FLAGS,
    CanaryController,
    FlagSet,
    SloReading,
)
from deployment import (  # noqa: E402
    CRITICAL,
    DAMAGED_REFUND,
    GatedTools,
    run_once,
)
from drill import drill_all, in_flight_containment  # noqa: E402
from gates import reliability, trajectory  # noqa: E402
from northstar_contracts import World  # noqa: E402
from rollout import walk  # noqa: E402
from shadow import compare, shadow_run  # noqa: E402
from versions import (  # noqa: E402
    V8,
    V9_GENEROUS,
    V9_GOOD,
    V9_MARGINAL,
    V9_REGRESSED,
    V9_UNSAFE,
    effective_config_hash,
)

BASELINE = str(Path(__file__).resolve().parent / "baselines" / "main.json")
INVARIANTS = [
    "issue_refund before get_policy",
    "issue_refund without idempotency_key",
]


def _reliability(version: str, min_pass_k: float = 0.90):  # noqa: ANN202
    """Invoke the reliability gate the way the CI workflow does."""
    return reliability.run(
        [
            "--scenarios", "critical",
            "--k", "5",
            "--min-pass-k", str(min_pass_k),
            "--baseline", BASELINE,
            "--max-regression", "0.02",
            "--version", version,
        ]
    )


def _trajectory(version: str):  # noqa: ANN202
    """Invoke the trajectory gate the way the CI workflow does."""
    return trajectory.run(
        [
            *sum((["--forbid", i] for i in INVARIANTS), []),
            "--require-state-grader",
            "--version", version,
        ]
    )


def test_reliability_gate_blocks_a_behavioural_regression() -> None:
    """A softened prompt does not change the code, and must still block."""
    good, good_code = _reliability(V9_GOOD.name)
    bad, bad_code = _reliability(V9_REGRESSED.name)

    assert good.passed and good_code == 0
    assert not bad.passed and bad_code == 1
    # The regression is in pass^k, measured, not asserted.
    worst = min(r.pass_k for r in bad.results)
    assert worst < min(r.pass_k for r in good.results)


def test_the_baseline_catches_what_an_absolute_floor_does_not() -> None:
    """The reason gates compare against the version they replace."""
    result, code = _reliability(V9_MARGINAL.name, min_pass_k=0.30)

    assert code == 1
    assert result.blocked_by_regression_only
    # Every scenario cleared the floor it was given.
    assert all(r.pass_k >= 0.30 for r in result.results)
    assert all(r.regression > result.max_regression for r in result.results)


def test_trajectory_gate_blocks_a_run_that_reached_the_right_state() -> None:
    """Same world state, wrong route, no key. Both invariants fire."""
    good, good_code = _trajectory(V9_GOOD.name)
    unsafe, unsafe_code = _trajectory(V9_UNSAFE.name)

    assert good.passed and good_code == 0
    assert not unsafe.passed and unsafe_code == 1
    assert len(unsafe.violations) == 2
    assert any("before" in v for v in unsafe.violations)
    assert any("idempotency_key" in v for v in unsafe.violations)

    # And the unsafe version still leaves the world correct, which is
    # exactly why a state grader alone would have let it through.
    outcome = run_once(V9_UNSAFE, DAMAGED_REFUND, deterministic=True)
    assert outcome.passed


def test_an_unparseable_invariant_is_refused_rather_than_ignored() -> None:
    """A gate that silently drops a rule reports green for the wrong reason."""
    with pytest.raises(ValueError, match="cannot parse invariant"):
        trajectory.parse_invariant("issue_refund should probably be careful")

    assert trajectory.parse_invariant("a before b").kind == "before"
    assert trajectory.parse_invariant("a without k").kind == "without"


def test_shadow_records_write_intents_and_executes_none() -> None:
    """Recorded, not dropped: that is what makes the diff possible."""
    baseline = shadow_run(V8, DAMAGED_REFUND)
    candidate = shadow_run(V9_GENEROUS, DAMAGED_REFUND)

    assert baseline.write_intents == 1
    assert candidate.write_intents == 1
    assert baseline.side_effects == 0
    assert candidate.side_effects == 0
    assert baseline.world.total_refunded_cents("NR-2026-0041827") == 0

    diff = compare(baseline, candidate)
    assert not diff.identical
    assert diff.only_baseline[0]["arguments"]["amount_cents"] == 3250
    assert diff.only_candidate[0]["arguments"]["amount_cents"] == 4900


def test_shadow_reads_still_reach_the_real_tools() -> None:
    """A shadow that stubs the reads is not running on real input."""
    run = shadow_run(V8, DAMAGED_REFUND)
    assert run.adapter.reads >= 2


def test_flags_are_independent() -> None:
    """One tool off leaves every other tool working."""
    world = World()
    flags = FlagSet()
    tools = GatedTools(flags).register_all(world.tools()).bind_world(world)
    spec = {s.name: s for s in world.tool_specs()}

    flags.disable("tool:issue_refund", reason="test")
    refund_ok, _ = flags.allows_call(spec["issue_refund"])
    read_ok, _ = flags.allows_call(spec["get_order"])
    message_ok, _ = flags.allows_call(spec["send_message"])

    assert not refund_ok
    assert read_ok and message_ok
    assert len(tools) == 6


def test_containment_reaches_runs_that_are_already_going() -> None:
    """The failure a real drill finds, reproduced both ways."""
    enforced = in_flight_containment(enforce=True)
    naive = in_flight_containment(enforce=False)

    assert enforced["runs_in_flight"] == naive["runs_in_flight"] > 0
    assert enforced["mutated_after_flip"] == 0
    assert naive["mutated_after_flip"] == naive["runs_in_flight"]


def test_every_declared_flag_has_a_drill_that_passes() -> None:
    """A switch with no drill is a hypothesis."""
    results = drill_all()
    drilled = {r.flag for r in results}

    assert len(drilled) >= len(FLAGS)
    assert all(r.passed for r in results), [
        r.observed for r in results if not r.passed
    ]


def test_the_canary_contains_instead_of_widening_on_a_breach() -> None:
    """Containment is a flag, not a deploy."""
    controller, observations = walk(V9_GENEROUS)

    assert controller.contained
    assert not controller.complete
    assert not controller.flags.enabled("all_mutations")
    assert controller.flags.actions[0]["flag"] == "all_mutations"
    # It was stopped at the read-only rung, before a single write.
    assert observations[-1].stage.cohort == COHORTS[0].cohort
    assert observations[-1].reading.mutations_attempted == 0


def test_a_good_candidate_reaches_full_exposure() -> None:
    """The ladder has to be climbable, or nobody will use it."""
    controller, observations = walk(V9_GOOD)

    assert controller.complete
    assert not controller.contained
    assert [o.stage.cohort for o in observations] == [
        c.cohort for c in COHORTS
    ]
    # The bounded rung deferred the over-ceiling ticket rather than
    # counting it as a failure.
    assert observations[1].deferred > 0


def test_slo_readings_are_ratios_over_counts() -> None:
    """Action integrity counts mutations, not requests."""
    reading = SloReading(
        cohort="beta",
        runs=10,
        verified_successes=9,
        mutations_attempted=8,
        mutations_correct=7,
    )
    assert reading.verified_success == 0.9
    assert reading.action_integrity == 0.875
    assert reading.breaches(CanaryController(FlagSet()).targets)


def test_a_prompt_only_edit_changes_the_configuration_hash() -> None:
    """If the prompt is not in the hash, the first incident question fails."""
    specs = World().tool_specs()
    assert V8.short_config_hash(specs) != V9_GOOD.short_config_hash(specs)

    unchanged = effective_config_hash(
        agent="v8", model="m", prompt="p", tools=specs
    )
    edited = effective_config_hash(
        agent="v8", model="m", prompt="p ", tools=specs
    )
    assert unchanged != edited


def test_the_critical_suite_grades_against_the_world() -> None:
    """Nothing in these gates reads a transcript."""
    for scenario in CRITICAL:
        outcome = run_once(V8, scenario, deterministic=True)
        assert outcome.grade.details["world"]
        assert outcome.passed
