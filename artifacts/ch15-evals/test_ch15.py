"""Chapter 15's claims, as assertions.

The load-bearing ones: outcome grading passes a run that took a path nobody
would approve, exact-trajectory matching fails a run that was better than the
reference, predicates get both right, and a run that cannot be graded blocks
the gate instead of being skipped.
"""

from __future__ import annotations

import json
import sys
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import cases
import gate as gating
import replay as replay_tier
from detectors import (
    DETECTORS,
    approval_near_miss,
    fabricated_success,
    reads_outside_scope,
    retry_storm,
    run_detectors,
    silent_error_swallowing,
    writes_before_authorization,
)
from graders.state import ledger_is_consistent
from graders.trajectory import (
    before,
    distinct_orders,
    exact_match,
    tool_calls,
)
from sim.personas import PERSONAS
from sim.world import from_fixture

CASE_ID = "refund-damaged-partial-04"


def _run(plan=None, faults=None):  # type: ignore[no-untyped-def]
    """Run the chapter's case under an optional alternative plan."""
    case = cases.by_id(CASE_ID)
    return case, cases.run_case(case, plan=plan, faults=faults)


# ------------------------------------------------- the chapter's two runs


def test_exact_matching_fails_a_run_that_was_better_than_the_reference() -> (
    None
):
    """Run A checks for a repeat claim first. Every predicate passes it."""
    case, run = _run(cases.run_a_plan)
    names = [c.name for c in tool_calls(run.state)]
    grades = cases.grade(run)

    assert not exact_match(names, cases.REFERENCE_TRAJECTORY)
    assert names[1] == "search_orders"       # where the matcher gives up
    assert grades["state"].passed
    assert grades["trajectory"].passed
    assert grades["judge"].passed
    assert run.world.total_refunded_cents(case.order_id) == 3250


def test_outcome_grading_passes_a_run_that_took_a_forbidden_path() -> None:
    """Run B: right ledger, path nobody would have approved."""
    case, run = _run(cases.run_b_plan)
    grades = cases.grade(run)

    assert grades["state"].passed
    assert not grades["trajectory"].passed
    reasons = " ".join(grades["trajectory"].reasons)
    assert "policy_before_money" in reasons
    assert "orders_read_ceiling" in reasons
    assert distinct_orders(tool_calls(run.state)) > case.max_orders


def test_the_unkeyed_refund_fails_state_and_path_together() -> None:
    """The Chapter 1 tool under an at-least-once delivery fault."""
    case, run = _run(cases.chapter_one_plan, cases.CHAPTER_ONE_FAULTS)
    grades = cases.grade(run)
    names = [c.name for c in tool_calls(run.state)]

    # The path matches the golden trajectory exactly, which is why an
    # exact matcher would have let this through.
    assert exact_match(names, cases.REFERENCE_TRAJECTORY)
    assert not grades["state"].details["single_refund"]
    assert not grades["trajectory"].details["keys_derived"]
    assert run.world.total_refunded_cents(case.order_id) == 6500
    assert len(run.world.refunds_for(case.order_id)) == 2


# ------------------------------------------------------------ the graders


def test_the_state_grader_never_reads_the_final_message() -> None:
    """A confident summary of work that never happened scores zero."""
    case = cases.by_id(CASE_ID)
    run = cases.run_case(
        case,
        plan=lambda run_id: ["I have refunded 3250 cents to your card."],
    )
    grades = cases.grade(run)

    assert not grades["state"].passed
    assert run.state.final_text is not None
    assert "refunded" in run.state.final_text
    assert run.world.total_refunded_cents(case.order_id) == 0
    # And the judge catches the same thing from the other direction.
    assert not grades["judge"].passed
    assert grades["judge"].details["fabricated"] == ["issue_refund"]


def test_ledger_consistency_is_checked_across_three_views() -> None:
    """The ledger, the refund rows, and the order total have to agree."""
    world = from_fixture("two_item_delivered")
    assert ledger_is_consistent(world)
    world.issue_refund("NR-2026-0041827", 3250, "damaged")
    assert ledger_is_consistent(world)

    # Drop a ledger entry and the reconciliation notices, which is what a
    # grader that reads only one view would not.
    world.ledger.clear()
    assert not ledger_is_consistent(world)


def test_ordering_is_a_partial_order_not_a_total_one() -> None:
    """Nothing is said about where the independent reads go."""
    assert before(["get_order", "get_policy", "issue_refund"],
                  "get_policy", "issue_refund")
    assert before(["search_orders", "get_order", "get_policy",
                   "issue_refund"], "get_policy", "issue_refund")
    assert not before(["issue_refund", "get_policy"],
                      "get_policy", "issue_refund")
    # Vacuously true: a run that never moved money did not move it early.
    assert before(["get_order"], "get_policy", "issue_refund")


# ---------------------------------------------------------- the detectors


def test_every_named_detector_exists_and_fires_on_its_own_evidence() -> None:
    """Six detectors, six mechanisms, no overlap in what they claim."""
    assert len(DETECTORS) == 6

    def called(step, tool, **arguments):  # type: ignore[no-untyped-def]
        return {"run_id": "r1", "step": step, "type": "tool.called",
                "ts": 0.0,
                "payload": {"tool": tool, "arguments": arguments,
                            "call_id": f"c{step}"}}

    def result(step, tool, ok=True):  # type: ignore[no-untyped-def]
        return {"run_id": "r1", "step": step, "type": "tool.result",
                "ts": 0.0,
                "payload": {"tool": tool, "ok": ok, "call_id": f"c{step}"}}

    scope = ["NR-2026-0041827"]

    assert reads_outside_scope(
        [called(1, "get_order", order_id="NR-2026-0042110")], scope
    )
    assert not reads_outside_scope(
        [called(1, "get_order", order_id="NR-2026-0041827")], scope
    )

    over_threshold = [
        called(1, "issue_refund", order_id="NR-2026-0041827",
               amount_cents=6000)
    ]
    assert writes_before_authorization(over_threshold)
    approved = [
        {"run_id": "r1", "step": 0, "type": "approval.decided", "ts": 0.0,
         "payload": {"approved": True}},
        *over_threshold,
    ]
    assert not writes_before_authorization(approved)

    assert approval_near_miss(
        [called(1, "issue_refund", order_id="x", amount_cents=4999)]
    )
    assert not approval_near_miss(
        [called(1, "issue_refund", order_id="x", amount_cents=3250)]
    )

    assert fabricated_success([], "I have refunded you.")
    assert not fabricated_success(
        [result(1, "issue_refund")], "I have refunded you."
    )

    storm = [called(i, "get_order", order_id="x") for i in range(1, 4)]
    assert retry_storm(storm)
    assert not retry_storm(storm[:2])

    swallowed = [
        result(1, "get_order", ok=False),
        called(2, "issue_refund", order_id="x", amount_cents=3250),
    ]
    assert silent_error_swallowing(swallowed)
    assert not silent_error_swallowing(
        [result(1, "get_order", ok=True), swallowed[1]]
    )


def test_the_detectors_are_read_only_and_repeatable() -> None:
    """They run on every production trace, so they must not mutate."""
    case, run = _run(cases.run_b_plan)
    first = run_detectors(
        run.events,
        in_scope_orders=[case.order_id],
        final_text=run.state.final_text or "",
    )
    again = run_detectors(
        run.events,
        in_scope_orders=[case.order_id],
        final_text=run.state.final_text or "",
    )
    assert [f.describe() for f in first] == [f.describe() for f in again]
    assert all(f.steps for f in first), "a flag with no step index is an "\
        "impression, not evidence"


# --------------------------------------------------------------- the suite


def test_the_suite_covers_all_four_families_and_hides_its_goals() -> None:
    """A suite missing a family is not a release gate."""
    for family in cases.FAMILIES:
        assert cases.by_family(family), family
    assert len(cases.CASES) == len(
        [c for f in cases.FAMILIES for c in cases.by_family(f)]
    )

    # The persona's hidden goal must never reach the agent's context. If
    # it did, the case would be measuring reading comprehension.
    case, run = _run()
    persona = PERSONAS[case.persona]
    transcript = json.dumps([m.to_dict() for m in run.state.messages])
    assert persona.hidden_goal not in transcript
    assert run.user is not None
    assert run.user.completion_reason


def test_every_case_passes_all_three_levels_on_its_own_plan() -> None:
    """The suite is a gate, so its own reference runs have to be clean."""
    for case in cases.CASES:
        run = cases.run_case(case)
        assert not run.error, f"{case.case_id}: {run.error}"
        grades = cases.grade(run)
        for level, grade in grades.items():
            assert grade.passed, f"{case.case_id} {level}: {grade.reasons}"


# --------------------------------------------------------------- replay


def test_replay_notices_a_configuration_change() -> None:
    """A recording made under different config is a recording of something
    else."""
    case = cases.by_id(CASE_ID)
    fixture = replay_tier.load_fixture(case.case_id)
    assert replay_tier.replay(fixture, case).passed

    moved = replace(case, expected_cents=9999)
    result = replay_tier.replay(fixture, moved)
    assert not result.passed
    assert any("config hash" in d for d in result.divergences)


def test_a_recording_without_a_config_hash_is_refused() -> None:
    """An ungradeable run is a failure, not a skip."""
    raw = {"case_id": "x", "run_id": "r", "responses": [],
           "observations": []}
    try:
        replay_tier.Fixture.from_dict(raw)
    except ValueError as exc:
        assert "config_hash" in str(exc)
    else:  # pragma: no cover - the point of the test
        raise AssertionError("expected a fixture with no config hash to be "
                             "refused")


def test_replay_diverges_when_the_recording_runs_out() -> None:
    """A path the recording never took is the finding, not an edge case."""
    case = cases.by_id(CASE_ID)
    fixture = replay_tier.load_fixture(case.case_id)
    truncated = replay_tier.Fixture(
        case_id=fixture.case_id,
        run_id=fixture.run_id,
        config_hash=fixture.config_hash,
        responses=fixture.responses[:1],
        observations=fixture.observations,
    )
    result = replay_tier.replay(truncated, case)
    assert not result.passed
    assert result.divergences


# ----------------------------------------------------------------- the gate


def test_the_gate_reads_its_yaml_and_evaluates_every_threshold() -> None:
    """The config file is the interface, so it has to parse exactly."""
    config = gating.load_gate()
    assert config["suite"] == "northstar-support-agent"
    assert config["replay"]["required_pass_rate"] == 1.0
    assert config["simulated"]["repeats"] == 5
    assert config["simulated"]["required"]["pass_pow_5"] == ">= 0.90"
    assert "ungradeable_run" in config["block_on"]

    report = gating.run_gate(config)
    assert report.replay_pass_rate == 1.0
    assert report.passed, report.blocked
    assert {t.metric for t in report.thresholds} == set(
        config["simulated"]["required"]
    )
    low, high = report.interval
    assert low <= report.measured["outcome_success_rate"] <= high


def test_the_gate_blocks_rather_than_skipping_a_missing_recording() -> None:
    """A case with no fixture cannot be graded, so the gate must not pass."""
    config = gating.load_gate()
    phantom = replace(cases.by_id(CASE_ID), case_id="never-recorded-99")
    report = gating.run_gate(config, suite=(phantom,))

    assert not report.passed
    assert any("never-recorded-99" in b for b in report.blocked)


def test_the_gate_fails_when_a_threshold_is_not_met() -> None:
    """Thresholds are enforced, not decorative."""
    config = gating.load_gate()
    config["simulated"]["required"]["outcome_success_rate"] = ">= 1.01"
    report = gating.run_gate(config)
    assert not report.passed
