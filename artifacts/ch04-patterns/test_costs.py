"""Pin the cost shape of every pattern, and the claim the chapter rests on.

The call counts are pinned rather than merely printed. A change that alters
a pattern's cost shape — an extra round trip inside a build, a plan that
stops riding along in context — should fail here rather than drift quietly
into a table nobody re-reads.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import pytest
import router
import task
from measure import PATTERNS, PatternCost, measure_all
from northstar_contracts import ToolSpec, World
from northstar_runtime import FakeModel
from planner import PLAN_CAP, PlanStep, validate
from verify import verify_refund

#: The one build that reads the system of record.
STATE_CHECK = "State verification"

#: Sequential model round trips per pattern on the clean fixture. Four tool
#: calls plus a final answer is five for the baseline; every other number is
#: what that pattern adds on top.
EXPECTED_CALLS = {
    "ReAct loop (baseline)": 5,
    "Router plus specialist": 6,
    "Plan-and-execute": 6,
    "Critic pass on the message": 6,
    "State verification": 5,
    "Best-of-3 plan search": 11,
}


@pytest.fixture(scope="module")
def clean() -> list[PatternCost]:
    """Every pattern, on a world with no fault injected."""
    return measure_all()


@pytest.fixture(scope="module")
def faulted() -> list[PatternCost]:
    """Every pattern, with the Chapter 1 refund timeout injected."""
    return measure_all(fault=True)


def test_call_counts_are_pinned(clean: list[PatternCost]) -> None:
    """A pattern that grows a round trip fails here, not in review."""
    measured = {c.name: c.model_calls for c in clean}
    assert measured == EXPECTED_CALLS


def test_every_pattern_produces_one_correct_refund(
    clean: list[PatternCost],
) -> None:
    """On the happy path the six differ only in what they cost."""
    assert len(clean) == len(PATTERNS)
    for cost in clean:
        assert cost.refund_rows == 1, cost.name
        assert cost.verified is True, cost.name
        assert cost.caught is False, cost.name


def test_routing_is_cheaper_and_search_is_not(
    clean: list[PatternCost],
) -> None:
    """Fewer tool specs in context beats one extra classification call."""
    by_name = {c.name: c for c in clean}
    base = by_name["ReAct loop (baseline)"].tokens
    assert by_name["Router plus specialist"].tokens < base
    assert by_name["Plan-and-execute"].tokens > base
    assert by_name["Best-of-3 plan search"].tokens > base
    # Verification adds no model call at all, so it adds no tokens.
    assert by_name[STATE_CHECK].tokens == base


def test_only_the_state_check_catches_the_duplicate(
    faulted: list[PatternCost],
) -> None:
    """The chapter's central claim, as an assertion on the ledger."""
    caught = {c.name for c in faulted if c.caught}
    assert caught == {STATE_CHECK}
    for cost in faulted:
        assert cost.refund_rows == 2, cost.name
        assert cost.verified is False, cost.name


def test_five_of_six_report_success_over_a_double_refund(
    faulted: list[PatternCost],
) -> None:
    """A green run is not evidence. That is why graders read the world."""
    succeeded = {c.name for c in faulted if c.status == "succeeded"}
    assert STATE_CHECK not in succeeded
    assert len(succeeded) == len(PATTERNS) - 1


def test_the_critic_approves_the_incident(
    faulted: list[PatternCost],
) -> None:
    """A critic inherits the blind spot of whatever you hand it."""
    critic = next(
        c for c in faulted if c.name == "Critic pass on the message"
    )
    assert critic.caught is False
    assert any(n.upper().startswith("APPROVE") for n in critic.notes)


def test_verify_refund_separates_two_different_bugs() -> None:
    """One row for the wrong amount is not two rows for the right total."""
    world = World()
    assert not verify_refund(world, task.ORDER_ID, 3250).ok  # no rows at all

    world.issue_refund(task.ORDER_ID, 1625, "damaged")
    wrong_amount = verify_refund(world, task.ORDER_ID, 3250)
    assert not wrong_amount.ok
    assert wrong_amount.content["refund_rows"] == 1

    world.issue_refund(task.ORDER_ID, 1625, "damaged")
    split = verify_refund(world, task.ORDER_ID, 3250)
    # The total is now right and the run is still wrong.
    assert split.content["total_cents"] == 3250
    assert split.content["refund_rows"] == 2
    assert not split.ok


def test_validate_rejects_a_plan_before_any_of_it_runs() -> None:
    """The part most implementations skip is the part that pays."""
    specs = {s.name: s for s in World().tool_specs()}

    writes_first = [
        PlanStep("issue_refund", "pay now", {}),
        PlanStep("get_order", "check later", {}),
    ]
    assert any(
        "writes before any read" in p
        for p in validate(writes_first, specs, PLAN_CAP)
    )

    unknown = [PlanStep("refund_everything", "invented", {})]
    assert any(
        "unknown tool" in p for p in validate(unknown, specs, PLAN_CAP)
    )

    too_long = [PlanStep("get_order", "again", {})] * (PLAN_CAP + 1)
    assert any(
        "exceeds the cap" in p for p in validate(too_long, specs, PLAN_CAP)
    )

    fine = [
        PlanStep("get_order", "read", {}),
        PlanStep("get_policy", "read", {}),
        PlanStep("issue_refund", "then pay", {}),
    ]
    assert validate(fine, specs, PLAN_CAP) == []


def test_an_unrecognised_route_fails_closed() -> None:
    """A hallucinated destination must not reach a dispatcher."""
    assert router.route(FakeModel(default=["refund"]), "x") == "refund"
    assert router.route(FakeModel(default=["billing"]), "x") == "fraud"
    assert router.route(FakeModel(default=["  REFUND "]), "x") == "refund"
    # The fallback is the branch that costs money and annoys nobody.
    assert router.ROUTES["fraud"] == ["escalate_to_specialist"]


def test_writes_flag_is_what_makes_a_plan_checkable() -> None:
    """``validate`` is only possible because every tool declares it."""
    specs: dict[str, ToolSpec] = {
        s.name: s for s in World().tool_specs()
    }
    assert specs["issue_refund"].writes is True
    assert specs["get_policy"].writes is False
