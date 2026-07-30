"""Chapter 14's claims, as assertions.

Four properties carry the chapter: the grader reads the world and not the
transcript, a forbidden action fails a run whose final state is correct,
attempts do not leak fixtures into one another, and the single-attempt
headline sits above the repeated-attempt number by an amount that lives in
the dual-control tasks.
"""

from __future__ import annotations

import sys
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from northstar_runtime import FakeModel

import report as reporting
from adapter import (
    COMPLIANCE,
    DualControl,
    attempt,
    plan_for,
    world_from_fixtures,
)
from contamination import check_tasks
from task import (
    BenchmarkTask,
    dual_control,
    holdout,
    load_tasks,
    solo,
    train,
)

TASKS = load_tasks()
BY_ID = {t.task_id: t for t in TASKS}
SEED = reporting.SEED


# ------------------------------------------------------------- the task set


def test_the_set_is_shaped_like_a_benchmark_and_not_a_pile_of_prompts() -> (
    None
):
    """Forty tasks, a frozen holdout, and provenance on every one."""
    assert len(TASKS) == 40
    assert len(dual_control(TASKS)) == 11
    assert len(solo(TASKS)) == 29
    assert len(holdout(TASKS)) + len(train(TASKS)) == len(TASKS)
    assert holdout(TASKS), "a suite with no frozen split cannot gate"

    for task in TASKS:
        assert task.provenance, task.task_id
        assert task.primary_order in task.initial_orders, task.task_id
        assert task.max_turns > 0 and task.budget_cents > 0, task.task_id

    # Escalation-only cases must forbid the refund tool, or "no money moved"
    # is an outcome the agent could satisfy by accident rather than by rule.
    for task in TASKS:
        if task.expect_escalation:
            assert "issue_refund" in task.forbidden_tools, task.task_id


def test_a_task_cannot_declare_a_contradictory_expectation() -> None:
    """The loader refuses a record no run could ever satisfy."""
    broken = BY_ID["nr-lamp-02"].to_dict()
    broken["forbidden_tools"] = ["issue_refund"]
    try:
        BenchmarkTask.from_dict(broken)
    except ValueError as exc:
        assert "forbid" in str(exc)
    else:  # pragma: no cover - the point of the test
        raise AssertionError("expected a contradictory task to be rejected")


def test_fixtures_are_scoped_to_the_task() -> None:
    """A task cannot pass by touching an order it was never given."""
    task = BY_ID["nr-fraud-01"]
    world = world_from_fixtures(task.initial_orders)
    assert set(world.orders) == set(task.initial_orders)
    assert "NR-2026-0041827" not in world.orders


# ---------------------------------------------------------------- grading


def test_the_grader_reads_the_ledger_and_not_the_final_message() -> None:
    """An agent that says it refunded and did not is a failure."""
    task = BY_ID["nr-lamp-02"]
    liar = FakeModel(
        default=["I have issued the refund of 3250 cents. All done."],
        strict=False,
    )
    result = attempt(task, liar, SEED)

    assert not result.passed
    assert result.cost_cents >= 0
    # The claim is in the transcript; the ledger is empty, and the ledger
    # is what the verdict came from.
    assert result.details["world"]["refund_count"] == 0


def test_a_forbidden_action_fails_a_run_that_reached_the_right_state() -> None:
    """Unsafe success: correct outcome, path nobody would have approved."""
    task = BY_ID["nr-lamp-02"]
    honest = attempt(task, None, SEED)
    assert honest.passed

    # Same expected state, but reading the whole order table is now out of
    # bounds for this case.
    restricted = replace(
        task, forbidden_tools=frozenset({"get_policy"})
    )
    result = attempt(restricted, None, SEED)

    assert not result.passed
    assert any("forbidden tool" in r for r in result.reasons)
    # The world still holds the right refund. Only the path was wrong.
    assert result.details["world"]["refund_count"] == 1


def test_attempts_do_not_leak_fixtures_into_one_another() -> None:
    """Five attempts at a refund task, five worlds, five identical ledgers."""
    task = BY_ID["nr-mug-01"]
    report = reporting.run_repeated(task, n=5, seed=SEED)

    assert len(report.attempts) == 5
    assert report.pass_1 == 1.0
    for a in report.attempts:
        assert a.details["world"]["refund_count"] == 1


# ------------------------------------------------------------ dual control


def test_a_dual_control_task_stalls_when_the_agent_never_asks() -> None:
    """The world only changes if the customer acts, and only if asked."""
    task = next(t for t in dual_control(TASKS) if t.expected_refund_cents)
    assert task.user_actions

    # The same plan with the request step removed: a competent-looking run
    # that never tells the customer what it needs from them.
    silent = FakeModel(
        default=plan_for(replace(task, user_actions=[])), strict=False
    )
    result = attempt(task, silent, SEED)

    assert not result.passed
    assert result.details["asked_for"] == []
    assert result.details["world"]["refund_count"] == 0


def test_asking_correctly_is_what_unblocks_the_write() -> None:
    """Compliance is probabilistic, but being asked is a precondition."""
    task = next(t for t in dual_control(TASKS) if t.expected_refund_cents)
    report = reporting.run_repeated(task, n=5, seed=SEED)

    assert any(a.passed for a in report.attempts)
    assert not all(a.passed for a in report.attempts)
    for a in report.attempts:
        assert a.details["asked_for"] == sorted(task.user_actions)

    # And the customer's answer is deterministic given the seed.
    first = DualControl(task.user_actions, seed=SEED, task_id=task.task_id)
    again = DualControl(task.user_actions, seed=SEED, task_id=task.task_id)
    body = "Could you send a photo of the damage?"
    assert first.request(body) == again.request(body)
    assert 0.0 < COMPLIANCE < 1.0


# ----------------------------------------------------- the two headline numbers


def test_the_headline_sits_above_the_release_number() -> None:
    """One attempt per task measures capability, not reliability."""
    reports = reporting.run_suite(TASKS, k=5, seed=SEED)
    summary = reporting.summarise(reports, k=5)

    assert set(summary) == {
        "headline_pass_at_1", "pass_pow_k", "p95_cost_cents"
    }
    assert summary["headline_pass_at_1"] > summary["pass_pow_k"]
    assert summary["p95_cost_cents"] > 0

    split = reporting.breakdown(reports, TASKS)
    assert split["solo"]["pass_1"] > split["dual_control"]["pass_1"]
    # The gap is concentrated in the guided tasks: solo work barely moves
    # between the two metrics, guided work collapses.
    solo_gap = split["solo"]["pass_1"] - split["solo"]["pass_pow_k"]
    dual_gap = (
        split["dual_control"]["pass_1"]
        - split["dual_control"]["pass_pow_k"]
    )
    assert dual_gap > solo_gap


def test_compare_exposes_the_signature_the_chapter_prints() -> None:
    """``compare(tasks, model, k)`` returns exactly the three keys."""
    result = reporting.compare(holdout(TASKS), None, k=3)
    assert set(result) == {
        "headline_pass_at_1", "pass_pow_k", "p95_cost_cents"
    }
    assert 0.0 <= result["pass_pow_k"] <= result["headline_pass_at_1"] <= 1.0


def test_percentile_is_nearest_rank_and_refuses_nonsense() -> None:
    """Cost and latency ceilings are decision inputs, so they must be right."""
    values = [float(v) for v in range(1, 101)]
    assert reporting.percentile(values, 95) == 95.0
    assert reporting.percentile(values, 50) == 50.0
    assert reporting.percentile([], 95) == 0.0
    try:
        reporting.percentile(values, 101)
    except ValueError:
        pass
    else:  # pragma: no cover - the point of the test
        raise AssertionError("expected a percentile outside [0, 100] to raise")


# ------------------------------------------------------------ contamination


def test_the_contamination_check_finds_pasted_public_text() -> None:
    """Clean on the shipped set, and loud on a task that copied one in."""
    assert check_tasks(TASKS) == []

    pasted = replace(
        BY_ID["nr-lamp-02"],
        task_id="nr-pasted-01",
        goal=(
            "You are a retail agent helping a user with their order and "
            "their return."
        ),
    )
    hits = check_tasks([pasted])
    assert hits
    assert hits[0].task_id == "nr-pasted-01"
    assert hits[0].field == "goal"
