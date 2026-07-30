"""Three runtimes, one trajectory, one refund — asserted, not asserted at.

Every test here reads the world or a measurement taken from a run. None of
them reads a status field, because all three ports report ``succeeded`` in
every configuration this file exercises, including the ones that would have
paid twice.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import pytest
import shared.triage as triage
from northstar_contracts import ToolCall
from northstar_evals import TrajectoryGrader
from scorecard import (
    PORT_CLASSES,
    PORTS,
    CountingPolicy,
    LocalSink,
    close_port,
    glue_lines,
    load_port,
    run_once,
    score_port,
)
from shared.triage import (
    AMOUNT_CENTS,
    EXPECTED_CALLS,
    GOAL,
    ORDER_ID,
    SPECS,
    fresh_world,
    refund_amounts,
    refund_key,
    registry,
)


@pytest.fixture(autouse=True)
def _clean_slate() -> None:
    """No port may see another port's checkpoints or sessions."""
    from ports.harness import reset_sessions

    triage.forget_checkpoints()
    reset_sessions()
    yield
    triage.forget_checkpoints()
    reset_sessions()


@pytest.mark.parametrize("port_name", PORTS)
def test_same_trajectory_one_refund(port_name: str) -> None:
    """The chapter's equivalence assertion, run against all three ports."""
    world = fresh_world()
    port = load_port(port_name)
    port.build(triage.model_for(), registry(world), SPECS)
    try:
        state = port.run(GOAL, run_id="run_01H3WAY")
        port.run(GOAL, run_id="run_01H3WAY")   # replay
    finally:
        close_port(port)
    assert TrajectoryGrader(EXPECTED_CALLS).grade(
        state, world).passed
    assert refund_amounts(world, ORDER_ID) == [AMOUNT_CENTS]


@pytest.mark.parametrize("port_name", PORTS)
def test_kill_mid_write_leaves_one_refund(port_name: str) -> None:
    """Criterion three, run rather than read off a documentation page.

    The worker dies with the refund committed and nothing recorded about
    it. Whatever the runtime does next — resume the turn, resume the run, or
    replay the whole thing — the ledger must still hold one row, because the
    key is derived from the run and the step and the refund service honours
    it.
    """
    score = score_port(port_name)
    assert score.refunds_after_kill == (AMOUNT_CENTS,)


@pytest.mark.parametrize("port_name", PORTS)
def test_replay_does_not_pay_twice(port_name: str) -> None:
    """Running the same run id again presents the same identity, not a new one."""
    score = score_port(port_name)
    assert score.refunds == (AMOUNT_CENTS,)
    assert score.refunds_after_replay == score.refunds


def test_a_generated_key_is_the_chapter_one_incident() -> None:
    """Change the derivation and the retry pays twice, in every runtime.

    This is the control for the tests above. Without it, three green ports
    would look like evidence that a framework provides the guarantee.

    The amount is half the ticket, because a second full refund would hit
    the world's own over-refund guard and the *guard* would be what stopped
    the duplicate. The derivation has to be the thing under test.
    """
    half = AMOUNT_CENTS // 2
    world = fresh_world()
    reg = registry(world)
    key = refund_key("run_01H3WAY")

    reg.dispatch(_refund_call("c3", key, half), run_id="run_01H3WAY", step=2)
    # A second attempt at the *same* intent, keyed the same way: no-op.
    reg.dispatch(_refund_call("c3", key, half), run_id="run_01H3WAY", step=2)
    assert refund_amounts(world, ORDER_ID) == [half]

    # The same second attempt with a per-attempt key: money moves again.
    reg.dispatch(
        _refund_call("c9", refund_key("run_01H3WAY", 99), half),
        run_id="run_01H3WAY",
        step=99,
    )
    assert refund_amounts(world, ORDER_ID) == [half, half]


def test_only_the_runtimes_you_own_expose_a_policy_hook() -> None:
    """Criterion one, measured: was your decision point actually asked?"""
    consulted: dict[str, bool] = {}
    for port_name in PORTS:
        world = fresh_world()
        policy = CountingPolicy()
        port, _ = run_once(port_name, world, policy=policy)
        close_port(port)
        consulted[port_name] = policy.saw_the_write

    assert consulted["raw"] is True
    assert consulted["graph"] is True
    # The hosted harness accepts the argument and drops it. That is what
    # "you can only wrap the tool" means when you measure it.
    assert consulted["harness"] is False


def test_checkpoint_granularity_differs_by_runtime() -> None:
    """The graph checkpoints per node; the harness, once per finished run."""
    scores = {name: score_port(name) for name in PORTS}
    assert scores["graph"].checkpoint_writes > scores["raw"].checkpoint_writes
    assert scores["raw"].checkpoint_writes > scores["harness"].checkpoint_writes
    # Finer checkpoints buy a later resume point, not a different ledger.
    assert (
        scores["graph"].resumed_from_step
        >= scores["raw"].resumed_from_step
        > scores["harness"].resumed_from_step
    )


def test_default_telemetry_egress_is_not_zero_everywhere() -> None:
    """Criterion seven: read what leaves the process before you adopt it."""
    scores = {name: score_port(name) for name in PORTS}
    assert scores["raw"].egress_bytes == 0
    assert scores["graph"].egress_bytes == 0
    assert scores["harness"].egress_bytes > 0


def test_glue_is_measured_from_source_not_asserted() -> None:
    """The cheapest runtime to assemble is not the one you can see into."""
    glue = {name: glue_lines(cls) for name, cls in PORT_CLASSES.items()}
    assert glue["harness"] < glue["raw"] < glue["graph"]


def test_local_sink_sees_nothing_the_harness_sends_out() -> None:
    """A collector you run only receives what the runtime hands it."""
    world = fresh_world()
    sink = LocalSink()
    port, _ = run_once("harness", world, telemetry=sink)
    close_port(port)
    assert sink.records == []


def _refund_call(call_id: str, key: str, cents: int = AMOUNT_CENTS):
    """One refund intent, keyed however the caller wants it keyed."""
    return ToolCall(
        call_id,
        "issue_refund",
        {
            "order_id": ORDER_ID,
            "amount_cents": cents,
            "idempotency_key": key,
        },
    )
