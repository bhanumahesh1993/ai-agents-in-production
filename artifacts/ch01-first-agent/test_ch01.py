"""The Chapter 1 incident, as assertions.

The demo prints; this fails a build. Same two properties either way.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from demo import AMOUNT, ORDER, run_broken, run_repaired
from northstar_contracts import World


def test_broken_tool_double_refunds_after_a_timeout() -> None:
    """A timeout is not a failure, and a blind retry proves it expensively."""
    world, agent = run_broken()

    # The run is happy. The world is not.
    assert agent.state.status == "succeeded"
    assert world.call_count("issue_refund") == 2
    assert len(world.refunds_for(ORDER)) == 2
    assert world.total_refunded_cents(ORDER) == AMOUNT * 2


def test_derived_key_collapses_the_retry() -> None:
    """Same trajectory, same fault, one refund."""
    world, agent = run_repaired()

    assert agent.state.status == "succeeded"
    # The tool is still called twice: the retry is not what we removed.
    assert world.call_count("issue_refund") == 2
    # What we removed is the second *effect*.
    assert len(world.refunds_for(ORDER)) == 1
    assert world.total_refunded_cents(ORDER) == AMOUNT


def test_the_transcript_cannot_tell_the_two_apart() -> None:
    """Why graders read state, not the agent's account of itself."""
    broken_world, broken_agent = run_broken()
    repaired_world, repaired_agent = run_repaired()

    assert broken_agent.state.status == repaired_agent.state.status
    assert broken_agent.trajectory() == repaired_agent.trajectory()
    assert broken_world.total_refunded_cents(
        ORDER
    ) != repaired_world.total_refunded_cents(ORDER)


def test_fault_injector_commits_before_it_raises() -> None:
    """The property the whole chapter rests on."""
    world = World()
    world.inject_fault("issue_refund", kind="timeout")
    try:
        world.issue_refund(ORDER, AMOUNT, "damaged")
    except Exception:
        pass
    # The write landed even though the caller saw an error.
    assert world.total_refunded_cents(ORDER) == AMOUNT
