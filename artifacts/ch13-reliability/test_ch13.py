"""Chapter 13's claims, as assertions.

The demo prints; this fails a build. The properties are the same either way:
the estimators agree with their closed forms, the repetitions are actually
independent, reliability falls with duration, and the shared-world harness
reproduces the artifact the chapter warns about.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import harness
import metrics
from compare import compare_versions
from error_budget import ErrorBudget
from northstar_contracts import Message, ToolCall
from tasks import CRITICAL_SET, by_bucket, by_id

N = 10
SEED = 1729


# --------------------------------------------------------------- estimators


def test_pass_k_is_the_ratio_of_binomial_coefficients() -> None:
    """The estimator, checked against the arithmetic rather than itself."""
    from math import comb

    results = [True] * 17 + [False] * 3
    for k in (1, 2, 4, 8):
        assert metrics.pass_k(results, k) == comb(17, k) / comb(20, k)

    # k=1 reduces to the plain success rate, which is why one function
    # serves both columns of the report.
    assert metrics.pass_k(results, 1) == 0.85
    # pass^k falls as k rises; pass@k rises. Opposite questions.
    assert metrics.pass_k(results, 4) < metrics.pass_k(results, 2)
    assert metrics.pass_at_k(results, 4) > metrics.pass_at_k(results, 2)


def test_wilson_reproduces_the_chapters_worked_values() -> None:
    """Nineteen of twenty is compatible with 76%, and five of five with 57%."""
    low, high = metrics.wilson(19, 20)
    assert round(low, 3) == 0.764
    assert round(high, 3) == 0.991

    perfect_low, perfect_high = metrics.wilson(5, 5)
    assert 0.56 < perfect_low < 0.57
    assert perfect_high == 1.0

    # More runs, same rate, tighter interval. The point of the table.
    narrow = metrics.wilson(435, 500)
    wide = metrics.wilson(87, 100)
    assert (narrow[1] - narrow[0]) < (wide[1] - wide[0])


def test_mcnemar_uses_only_the_discordant_pairs() -> None:
    """Northstar's 18-to-6 split, and the concordant pairs it ignores."""
    assert round(metrics.mcnemar_exact(18, 6), 3) == 0.023
    # No disagreement is no evidence, not a significant tie.
    assert metrics.mcnemar_exact(0, 0) == 1.0
    # The test is symmetric: which arm won does not change the p value.
    assert metrics.mcnemar_exact(6, 18) == metrics.mcnemar_exact(18, 6)


def test_bootstrap_over_tasks_is_wider_than_the_pooled_interval() -> None:
    """Twelve tasks are twelve samples, whatever the run count says."""
    per_task = [1.0, 1.0, 0.95, 0.9, 0.85, 0.8, 0.7, 0.7, 0.6, 0.55, 0.5, 0.4]
    low, high = metrics.bootstrap_over_tasks(per_task)
    mean = sum(per_task) / len(per_task)
    assert low < mean < high

    pooled = metrics.wilson(int(mean * 240), 240)
    assert (high - low) > (pooled[1] - pooled[0])


# ------------------------------------------------------------------- plans


def test_a_wasted_turn_costs_a_turn_and_not_a_step() -> None:
    """The plan re-derives its next action from what actually landed.

    This is what separates the measurement from a positional script, where
    every interruption is fatal and every task decays at the same rate.
    """
    plan = by_id("damaged-single-item").plan
    first = plan.steps[0]

    assert plan([]) == first

    # A successful but *unplanned* read: the stall the flaky model emits.
    wasted = ToolCall("stall-1", "get_policy", {})
    conversation = [
        Message(role="assistant", content=[{"type": "tool_use",
                                            "id": wasted.id,
                                            "name": wasted.name,
                                            "input": wasted.arguments}]),
        Message(role="tool", content={"call_id": wasted.id,
                                      "tool": wasted.name, "ok": True}),
    ]
    assert plan(conversation) == first          # no progress lost

    # The planned first step, this time. Now the cursor moves.
    conversation += [
        Message(role="assistant", content=[{"type": "tool_use",
                                            "id": first.id,
                                            "name": first.name,
                                            "input": first.arguments}]),
        Message(role="tool", content={"call_id": first.id,
                                      "tool": first.name, "ok": True}),
    ]
    assert plan(conversation) == plan.steps[1]


def test_a_failed_step_does_not_advance_the_plan() -> None:
    """An error is not progress, however confident the transcript sounds."""
    plan = by_id("damaged-single-item").plan
    first = plan.steps[0]
    conversation = [
        Message(role="assistant", content=[{"type": "tool_use",
                                            "id": first.id,
                                            "name": first.name,
                                            "input": first.arguments}]),
        Message(role="tool", content={"call_id": first.id,
                                      "tool": first.name, "ok": False}),
    ]
    assert plan(conversation) == first
    assert plan.cursor(conversation) == 0


def test_every_task_declares_a_bucket_its_plan_supports() -> None:
    """Short means six turns or fewer; long means twelve or more."""
    assert len(CRITICAL_SET) == 12
    assert len(by_bucket("short")) == 6
    assert len(by_bucket("long")) == 6
    for task in by_bucket("short"):
        assert task.plan.turns <= 6, task.id
    for task in by_bucket("long"):
        assert task.plan.turns >= 12, task.id
    # No two adjacent steps are identical, so the flaky model's verbatim
    # repeat of the call it just made can never be mistaken for the next
    # step of the plan. That is the invariant the cursor depends on.
    for task in CRITICAL_SET:
        steps = task.plan.steps
        for before, after in zip(steps, steps[1:], strict=False):
            assert (before.name, before.arguments) != (
                after.name, after.arguments
            ), task.id


# ---------------------------------------------------------------- the harness


def test_fresh_fixtures_per_repetition_keep_a_correct_agent_scoring() -> None:
    """The load-bearing line of ``harness.py``, asserted both ways."""
    task = by_id("damaged-single-item")

    correct = harness.run_repeated(task, n=N, seed=SEED)
    shared = harness.run_shared_world_suite(
        (task,), n=N, seed=SEED
    ).reports[0]

    assert correct.n == shared.n == N
    assert correct.successes > shared.successes
    # The broken harness passes the first run and then poisons every one
    # after it, which is exactly why it reads as an agent regression.
    assert shared.successes <= 1
    assert correct.pass_1 > 0.5


def test_reliability_falls_with_task_duration() -> None:
    """The bucketed view, which is what picks a launch scope."""
    short = harness.run_suite(by_bucket("short"), n=N, seed=SEED)
    long_ = harness.run_suite(by_bucket("long"), n=N, seed=SEED)

    assert short.pass_1 > long_.pass_1
    # And the gap widens under pass^k, because the decay is exponential.
    assert (short.pass_k - long_.pass_k) > (short.pass_1 - long_.pass_1)


def test_pass_k_never_exceeds_pass_1_on_any_task() -> None:
    """A property of the estimator that has to survive real runs."""
    suite = harness.run_suite(CRITICAL_SET, n=N, seed=SEED)
    for report in suite.reports:
        assert report.pass_k_values[4] <= report.pass_1 + 1e-12, report.task
    assert 0.0 < suite.pass_1 < 1.0


def test_the_measurement_is_reproducible_at_a_fixed_seed() -> None:
    """If the seed moves the headline more than the interval, it is noise."""
    task = by_id("damaged-two-items")
    first = harness.run_repeated(task, n=N, seed=SEED)
    again = harness.run_repeated(task, n=N, seed=SEED)
    assert first.results == again.results


def test_the_recovery_task_survives_a_commit_then_timeout() -> None:
    """A derived key is what turns Chapter 1's fault into a recovery case."""
    task = by_id("refund-after-timeout")
    assert task.faults == (("issue_refund", "timeout"),)
    report = harness.run_repeated(task, n=N, seed=SEED)
    # Without the key every single run would double-refund and score zero.
    assert report.successes > 0


# ------------------------------------------------------------- comparison


def test_a_paired_comparison_accounts_for_every_run() -> None:
    """Pairing is only honest if the two arms saw the same draws."""
    result = compare_versions(by_bucket("long"), n=N, seed=SEED)
    total = sum(r.n for r in result.baseline.reports)

    assert result.concordant + result.discordant == total
    assert result.candidate.pass_1 > result.baseline.pass_1
    assert result.candidate_wins > result.baseline_wins
    assert 0.0 <= result.p_value <= 1.0
    low, high = result.delta_interval
    assert low <= result.delta <= high or low <= 0.0 <= high


# ----------------------------------------------------------- error budget


def test_the_error_budget_turns_a_rate_into_a_launch_decision() -> None:
    """Same objective, same rate, different volume, different answer."""
    budget = ErrorBudget(objective=0.99, volume=400)
    assert round(budget.budget_failures, 6) == 4.0

    whole = budget.assess("whole suite", 0.854)
    assert round(whole.expected_failures, 1) == 58.4
    assert not whole.inside_budget

    short = budget.scaled_to(240).assess("short bucket", 0.992)
    assert round(short.expected_failures, 1) == 1.9
    assert short.inside_budget
    assert short.headroom > 0
