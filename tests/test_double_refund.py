"""The Chapter 1 incident, and the three things that fix it.

This is the spine of the book expressed as tests. One script, four
harnesses, four outcomes:

* an unprotected loop refunds the customer twice;
* stamping idempotency keys and retrying inside the harness refunds once;
* a durable journal survives a worker crash without repeating the refund;
* and the world itself proves it, because we assert on the store rather
  than on what the agent said.
"""

from __future__ import annotations

from collections.abc import Sequence

import pytest
from northstar_contracts import Message, ToolCall, World
from northstar_evals import StateGrader, trajectory
from northstar_runtime import (
    AgentLoop,
    DurableRunner,
    FakeModel,
    MemoryJournal,
    SimulatedCrash,
    ToolRegistry,
)

from conftest import DELIVERED_ORDER

REFUND_CENTS = 4200


def _refund_call(call_id: str) -> ToolCall:
    """The refund the agent wants to make."""
    return ToolCall(
        call_id,
        "issue_refund",
        {
            "order_id": DELIVERED_ORDER,
            "amount_cents": REFUND_CENTS,
            "reason": "damaged",
        },
    )


def _blind_retry(messages: Sequence[Message]) -> ToolCall | str:
    """Retry the refund if and only if the last observation failed.

    This is the whole behaviour under test, and it is not a strawman: an
    agent that sees ``ToolTimeout`` and tries again is doing the obvious
    thing. The bug is that the runtime gave it no way to tell "the refund
    did not happen" from "the refund happened and the reply was lost".
    """
    last = messages[-1]
    if (
        last.role == "tool"
        and isinstance(last.content, dict)
        and not last.content["ok"]
    ):
        return _refund_call("c3")
    return "Your refund is complete."


#: One script. The branch is driven by what the agent observes, so the
#: incident and the repair are the same agent, not two.
SCRIPT: list[object] = [
    ToolCall("c1", "get_order", {"order_id": DELIVERED_ORDER}),
    _refund_call("c2"),
    _blind_retry,
    "Your refund is complete.",
]


def _run(world: World, *, stamp_keys: bool) -> tuple[AgentLoop, object]:
    """Run the script against a world whose refund call times out."""
    world.inject_fault("issue_refund", kind="timeout")
    registry = ToolRegistry(
        inject_idempotency_key=stamp_keys
    ).register_all(world.tools())
    loop = AgentLoop(model=FakeModel(default=SCRIPT), tools=registry)
    return loop, loop.run("refund the faulty headphones", run_id="run-1")


def test_unprotected_retry_double_refunds(world: World) -> None:
    """Without an idempotency key, the blind retry refunds twice."""
    loop, state = _run(world, stamp_keys=False)

    assert world.total_refunded_cents(DELIVERED_ORDER) == 2 * REFUND_CENTS
    assert len(world.refunds_for(DELIVERED_ORDER)) == 2
    # And the run reports success, which is the part that hurts: nothing
    # in the transcript or the status field says anything went wrong.
    assert state.status == "succeeded"
    assert trajectory(state) == [
        "get_order",
        "issue_refund",
        "issue_refund",
    ]


def test_state_grader_catches_what_the_transcript_hides(
    world: World,
) -> None:
    """The outcome grader fails the run the status field passed."""
    _, state = _run(world, stamp_keys=False)

    result = (
        StateGrader()
        .refunded(DELIVERED_ORDER, REFUND_CENTS)
        .no_duplicate_refunds(DELIVERED_ORDER)
        .grade(state, world)
    )

    assert not result.passed
    assert any("refund row" in reason for reason in result.reasons)


def test_idempotency_key_makes_the_retry_a_no_op(world: World) -> None:
    """With a key, the harness retries and the money moves once."""
    loop, state = _run(world, stamp_keys=True)

    assert world.total_refunded_cents(DELIVERED_ORDER) == REFUND_CENTS
    assert len(world.refunds_for(DELIVERED_ORDER)) == 1
    assert state.status == "succeeded"
    # The model never saw a failure, so it never retried: the harness
    # absorbed the timeout at the point where it had enough information
    # to do so safely.
    assert trajectory(state) == ["get_order", "issue_refund"]
    assert (
        StateGrader()
        .refunded(DELIVERED_ORDER, REFUND_CENTS)
        .no_duplicate_refunds(DELIVERED_ORDER)
        .grade(state, world)
        .passed
    )


def test_the_world_saw_two_attempts_and_moved_money_once(
    world: World,
) -> None:
    """Idempotency is not "the call happened once"; it is "the effect did"."""
    _run(world, stamp_keys=True)

    assert world.call_count("issue_refund") == 2
    assert len(world.effects("refund_issued")) == 1


def test_registry_knows_which_calls_are_retry_safe(world: World) -> None:
    """The retry decision is a property of the contract, not a guess."""
    unkeyed = ToolRegistry().register_all(world.tools())
    keyed = ToolRegistry(inject_idempotency_key=True).register_all(
        world.tools()
    )
    refund = _refund_call("c1")
    read = ToolCall("c2", "get_order", {"order_id": DELIVERED_ORDER})

    assert unkeyed.is_retry_safe(read)
    assert not unkeyed.is_retry_safe(refund)
    assert keyed.is_retry_safe(refund)


def test_crash_after_refund_does_not_refund_again(world: World) -> None:
    """A worker that dies after the write resumes without repeating it."""
    journal = MemoryJournal()
    runner = DurableRunner(
        model=FakeModel(
            default=[
                ToolCall("c1", "get_order", {"order_id": DELIVERED_ORDER}),
                _refund_call("c2"),
                ToolCall(
                    "c3",
                    "send_message",
                    {"order_id": DELIVERED_ORDER, "body": "Refunded."},
                ),
                "Your refund is complete.",
            ]
        ),
        tools=world.tools(),
        journal=journal,
    )

    with pytest.raises(SimulatedCrash):
        runner.start("refund", run_id="run-1", crash_after_step=2)

    assert world.total_refunded_cents(DELIVERED_ORDER) == REFUND_CENTS
    assert world.messages == []

    state = runner.resume("run-1")

    assert state.status == "succeeded"
    assert world.total_refunded_cents(DELIVERED_ORDER) == REFUND_CENTS
    assert len(world.refunds_for(DELIVERED_ORDER)) == 1
    assert len(world.messages) == 1
    # The refund tool was invoked exactly once across both processes: the
    # replay served the journaled result instead of calling it again.
    assert world.call_count("issue_refund") == 1


def test_duplicate_delivery_is_absorbed_by_the_key(world: World) -> None:
    """An at-least-once gateway replaying the request moves money once."""
    world.inject_fault("issue_refund", kind="duplicate")
    registry = ToolRegistry(inject_idempotency_key=True).register_all(
        world.tools()
    )
    loop = AgentLoop(
        model=FakeModel(default=[_refund_call("c1"), "Done."]),
        tools=registry,
    )
    loop.run("refund", run_id="run-1")

    assert world.total_refunded_cents(DELIVERED_ORDER) == REFUND_CENTS


def test_duplicate_delivery_without_a_key_moves_money_twice(
    world: World,
) -> None:
    """The same fault, unprotected, is the same incident by another route."""
    world.inject_fault("issue_refund", kind="duplicate")
    loop = AgentLoop(
        model=FakeModel(default=[_refund_call("c1"), "Done."]),
        tools=world.tools(),
    )
    loop.run("refund", run_id="run-1")

    assert world.total_refunded_cents(DELIVERED_ORDER) == 2 * REFUND_CENTS
