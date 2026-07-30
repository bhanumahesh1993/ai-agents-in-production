"""The durable-execution contract, as assertions on behaviour.

Four obligations, and each one has a test that fails if it stops holding:
the journal is append-only and records an effect *before* it counts as done;
replay rebuilds state without re-executing anything; a step whose intent was
recorded and whose outcome was not is re-issued under the same derived key,
so the effect count stays at one; and a divergence in the step *sequence*
raises loudly instead of being swallowed.

Every assertion here reads the world's refund ledger or the journal, never
the run's own account of itself. A run that reports ``succeeded`` while the
ledger holds two refunds is the failure this whole chapter exists to remove,
and a test that grades the transcript cannot see it.
"""

from __future__ import annotations

import corpus
import crash
import pytest
import unsafe
from northstar_contracts import World, idempotency_key
from northstar_runtime import (
    DurableRunner,
    FakeModel,
    MemoryJournal,
    ReplayDivergence,
    SimulatedCrash,
)
from stream import LAST_EVENT_ID_HEADER, StreamClient, event_id, stream
from workflow import (
    APPROVAL_THRESHOLD_CENTS,
    CRASH_POINTS,
    RunContext,
    Suspended,
    refund_workflow,
    step_record,
)


def types_of(records: list[dict]) -> list[str]:
    """The record types of a journal, in order."""
    return [r["type"] for r in records]


def steps_of(records: list[dict], type: str) -> list[str]:
    """The step ids of every record of one type, in order."""
    return [
        r["payload"]["step_id"]
        for r in records
        if r["type"] == type and "step_id" in r["payload"]
    ]


# ----------------------------------------------------- the journal's ordering


def test_the_intent_is_journaled_before_the_effect_counts_as_done(
    world: World,
) -> None:
    """``step.started`` precedes the call; ``step.completed`` follows it.

    The window between them is the ambiguity window, and a journal that
    only records outcomes cannot describe the state a run was in while a
    call was in flight.
    """
    run = crash.start("run-order", world=world)
    records = run.records()
    started = records.index(
        next(r for r in records if steps_of([r], "step.started") == ["issue_refund"])
    )
    completed = records.index(
        next(
            r
            for r in records
            if steps_of([r], "step.completed") == ["issue_refund"]
        )
    )
    assert started < completed
    assert types_of(records)[0] == "step.started"


def test_a_crash_inside_the_window_leaves_an_intent_with_no_outcome(
    world: World,
) -> None:
    """The effect landed. Nothing recorded that it did. That is a question."""
    run = crash.start(
        "run-window", crash_at="after_refund_commit", world=world
    )
    assert run.outcome == "crashed"
    assert run.unresolved() == ["issue_refund"]
    # The money moved before the worker died, which is why the resume may
    # not simply repeat the step.
    assert world.total_refunded_cents(crash.ORDER) == crash.LAMP_SHADE_CENTS


def test_the_journal_refuses_a_record_type_it_does_not_know() -> None:
    """A journal is a contract, not a log file."""
    with pytest.raises(ValueError, match="unknown step type"):
        step_record("run-x", 0, "step.maybe", {})


# ---------------------------------------------------- resolve, do not repeat


def test_resuming_the_money_window_leaves_exactly_one_refund(
    world: World,
) -> None:
    """The property the whole artifact exists to prove."""
    first = crash.start(
        "run-resolve", crash_at="after_refund_commit", world=world
    )
    resumed = crash.resume(first)

    assert resumed.refund_rows == 1
    assert resumed.refunded_cents == crash.LAMP_SHADE_CENTS
    assert resumed.state is not None
    assert resumed.state.status == "succeeded"
    # The two reads came back from the record; only the unresolved write ran.
    assert resumed.replayed == ["get_order", "get_policy"]
    assert resumed.executed == ["issue_refund"]


def test_the_reissued_call_is_recognised_rather_than_paid(
    world: World,
) -> None:
    """The refund service returned the original receipt, not a second one."""
    first = crash.start(
        "run-receipt", crash_at="after_refund_commit", world=world
    )
    resumed = crash.resume(first)
    outcomes = [
        r["payload"]["result"]
        for r in resumed.records()
        if r["type"] == "step.completed"
        and r["payload"]["step_id"] == "issue_refund"
    ]
    assert outcomes, "the resumed run recorded no refund outcome"
    assert outcomes[-1]["duplicate"] is True
    assert len(world.refunds_for(crash.ORDER)) == 1


@pytest.mark.parametrize(
    "point", [p for p in CRASH_POINTS if p != "mid_stream"]
)
def test_every_crash_point_resumes_to_one_refund(point: str) -> None:
    """Four injection points, four journals, one refund every time."""
    approve = point == "during_approval_wait"
    order = crash.FLAGGED_ORDER if approve else crash.ORDER
    amount = crash.FLAGGED_CENTS if approve else crash.LAMP_SHADE_CENTS

    first = crash.start(
        f"run-{point}", order_id=order, amount_cents=amount, crash_at=point
    )
    resumed = crash.resume(
        first, order_id=order, amount_cents=amount, approve=approve
    )
    assert resumed.refund_rows == 1
    assert resumed.refunded_cents == amount


# -------------------------------------------------------- the derived key


def test_the_key_is_a_pure_function_of_run_and_step(world: World) -> None:
    """A second worker computes the same key without ever having stored it."""
    journal = MemoryJournal()
    first = RunContext(run_id="run-key", journal=journal, world=world)
    second = RunContext(run_id="run-key", journal=journal, world=World())

    assert first.key_for("refund") == second.key_for("refund")
    assert first.key_for("refund") == idempotency_key(
        "run-key", "run-key:refund"
    )


def test_a_nonce_is_not_an_idempotency_key(world: World) -> None:
    """The failure the derivation prevents, reproduced so it stays visible."""
    first = crash.start(
        "run-nonce",
        crash_at="after_refund_commit",
        unsafe_key=True,
        world=world,
    )
    resumed = crash.resume(first, unsafe_key=True)

    assert resumed.refund_rows == 2
    assert resumed.refunded_cents == 2 * crash.LAMP_SHADE_CENTS


def test_the_silent_divergence_is_a_value_not_a_sequence() -> None:
    """The wall-clock and nonce versions drift; the journaled ones do not."""
    result = unsafe.compare("run-clock", crash.ORDER)
    assert result["safe_is_stable"] is True
    assert result["broken_is_stable"] is False


# ---------------------------------------------------------- replay is strict


def test_replaying_the_corpus_executes_nothing() -> None:
    """A replay that touches the world is not a replay."""
    results = corpus.replay_all()
    assert len(results) >= 4
    for result in results:
        assert result["executed"] == []
        assert result["world_untouched"] is True


def test_a_changed_step_sequence_raises_loudly() -> None:
    """The loud class of divergence, which is the good outcome."""
    entry = corpus.load_corpus()[0]
    mutated = corpus.CorpusEntry(
        name=entry.name + "-mutated",
        run_id=entry.run_id,
        order_id=entry.order_id,
        amount_cents=entry.amount_cents,
        records=[
            {
                **record,
                "payload": {
                    **record["payload"],
                    **(
                        {"step_id": "check_fraud"}
                        if record["payload"].get("step_id") == "get_policy"
                        else {}
                    ),
                },
            }
            for record in entry.records
        ],
    )
    with pytest.raises(ReplayDivergence, match="different path"):
        corpus.replay(mutated)


def test_an_incomplete_journal_replays_to_where_the_worker_died() -> None:
    """Not a failure. The first question in any incident."""
    interrupted = [
        r for r in corpus.replay_all() if r["incomplete"]
    ]
    assert interrupted, "the corpus no longer holds an interrupted journal"
    assert all(r["status"] == "interrupted" for r in interrupted)


# ---------------------------------------------------------- the human wait


def test_the_wait_parks_the_run_and_moves_no_money(world: World) -> None:
    """Suspension releases the worker. It does not pre-authorise anything."""
    parked = crash.start(
        "run-park",
        order_id=crash.FLAGGED_ORDER,
        amount_cents=crash.FLAGGED_CENTS,
        world=world,
    )
    assert parked.outcome == "suspended"
    assert parked.refund_rows == 0
    assert world.ledger == []

    resumed = crash.resume(
        parked, amount_cents=crash.FLAGGED_CENTS, approve=True
    )
    assert resumed.refunded_cents == crash.FLAGGED_CENTS
    assert resumed.refund_rows == 1


def test_a_refund_at_the_threshold_still_asks(world: World) -> None:
    """``>=``, not ``>``. A threshold you can sit on is one someone sits on."""
    ctx = RunContext(run_id="run-threshold", world=world)
    with pytest.raises(Suspended):
        refund_workflow(ctx, crash.ORDER, APPROVAL_THRESHOLD_CENTS)
    assert world.total_refunded_cents(crash.ORDER) == 0


# ------------------------------------------------------- the resumable stream


def test_a_reconnect_delivers_what_it_missed_with_no_gap(
    world: World,
) -> None:
    """Event granularity, monotonic per run, resumed from ``Last-Event-ID``."""
    run = crash.resume(crash.start("run-sse", world=world))
    total = len(run.records())

    client = StreamClient(run.run_id)
    first = client.consume(stream(run.journal, run.run_id, crash_after=3))
    assert first == 3
    assert client.headers()[LAST_EVENT_ID_HEADER] == str(client.last_event_id)

    client.consume(stream(run.journal, run.run_id, client.last_event_id))
    assert len(client.ids) == total
    assert client.gapless is True


def test_event_ids_start_at_one() -> None:
    """A client that has seen nothing sends 0, so ids cannot start there."""
    assert event_id({"seq": 0}) == 1


# ------------------------------------------------- the same contract, engine


def test_the_loop_shaped_engine_holds_the_same_property() -> None:
    """``DurableRunner`` crashes, resumes, and leaves one refund."""
    world = World()
    from northstar_contracts import ToolCall

    script = [
        ToolCall("c1", "get_order", {"order_id": crash.ORDER}),
        ToolCall("c2", "get_policy", {"reason": "damaged"}),
        ToolCall(
            "c3",
            "issue_refund",
            {
                "order_id": crash.ORDER,
                "amount_cents": crash.LAMP_SHADE_CENTS,
                "reason": "damaged",
            },
        ),
        "Refunded the cracked lamp shade.",
    ]
    runner = DurableRunner(
        model=FakeModel(default=script), tools=world.tools(), max_turns=8
    )
    with pytest.raises(SimulatedCrash):
        runner.start("refund the lamp shade", run_id="run-engine",
                     crash_after_step=3)

    state = runner.resume("run-engine")
    assert state.status == "succeeded"
    assert len(world.refunds_for(crash.ORDER)) == 1
    assert world.total_refunded_cents(crash.ORDER) == crash.LAMP_SHADE_CENTS

    # Replay rebuilds the state and executes nothing further.
    before = len(world.refunds_for(crash.ORDER))
    rebuilt = runner.replay("run-engine")
    assert rebuilt.status == "succeeded"
    assert len(world.refunds_for(crash.ORDER)) == before
