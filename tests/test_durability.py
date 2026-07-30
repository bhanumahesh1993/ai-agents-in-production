"""Checkpointing, journalling, replay, resume, cancel, and human waits."""

from __future__ import annotations

from pathlib import Path

import pytest
from northstar_contracts import Message, RunState, ToolCall, World
from northstar_policy import ApprovalStore, Principal, default_northstar_policy
from northstar_runtime import (
    AgentLoop,
    DurableRunner,
    FakeModel,
    FileJournal,
    MemoryCheckpointer,
    MemoryJournal,
    RunCancelled,
    SimulatedCrash,
    SqliteCheckpointer,
)

from conftest import DAMAGED_ORDER, DELIVERED_ORDER, refund_script

# ------------------------------------------------------------- checkpointers


@pytest.mark.parametrize("factory", [MemoryCheckpointer, SqliteCheckpointer])
def test_checkpointers_round_trip(factory: type) -> None:
    """Both implementations satisfy the same three-method contract."""
    checkpointer = factory()
    try:
        state = RunState(
            run_id="run-1",
            step=3,
            messages=[Message("user", "refund the mug")],
            status="waiting_approval",
            budget_spent_cents=17,
        )
        checkpointer.save(state)
        loaded = checkpointer.load("run-1")

        assert loaded == state
        assert checkpointer.load("run-missing") is None
    finally:
        if hasattr(checkpointer, "close"):
            checkpointer.close()


def test_checkpoint_stores_a_copy_not_a_reference() -> None:
    """Later mutation of the live object must not rewrite history."""
    checkpointer = MemoryCheckpointer()
    messages = [Message("user", "hello")]
    state = RunState(run_id="run-1", messages=messages)
    checkpointer.save(state)
    messages.append(Message("user", "and another thing"))

    loaded = checkpointer.load("run-1")
    assert loaded is not None
    assert len(loaded.messages) == 1


def test_sqlite_checkpointer_survives_a_new_connection(
    tmp_path: Path,
) -> None:
    """The point of the SQLite one: it outlives the process."""
    path = tmp_path / "runs.db"
    with SqliteCheckpointer(path) as first:
        first.save(RunState(run_id="run-1", step=5, status="waiting_approval"))

    with SqliteCheckpointer(path) as second:
        loaded = second.load("run-1")
        assert loaded is not None
        assert loaded.step == 5
        assert second.run_ids(status="waiting_approval") == ["run-1"]


def test_checkpoint_history_shows_every_step(world: World) -> None:
    """After the fact, a run can be walked step by step."""
    checkpointer = MemoryCheckpointer()
    loop = AgentLoop(
        model=FakeModel(default=refund_script()),
        tools=world.tools(),
        checkpointer=checkpointer,
    )
    loop.run("refund the mug", run_id="run-1")
    history = checkpointer.history("run-1")

    assert [s.step for s in history] == [0, 1, 2, 3, 4]
    assert history[-1].status == "succeeded"


def test_resume_from_a_checkpoint_in_a_second_process(
    world: World,
) -> None:
    """A fresh loop, the stored state, and the run carries on."""
    checkpointer = MemoryCheckpointer()
    first = AgentLoop(
        model=FakeModel(default=refund_script()),
        tools=world.tools(),
        checkpointer=checkpointer,
    )
    state = first.start("refund the damaged mug", run_id="run-1")
    state = first.step(state)
    assert state.step == 1

    # The worker dies here. A different worker picks the run up.
    second = AgentLoop(
        model=FakeModel(default=refund_script()),
        tools=world.tools(),
        checkpointer=checkpointer,
    )
    loaded = checkpointer.load("run-1")
    assert loaded is not None
    finished = second.resume(loaded)

    assert finished.status == "succeeded"
    assert world.total_refunded_cents(DAMAGED_ORDER) == 3250


def test_resumed_run_does_not_get_a_fresh_budget(world: World) -> None:
    """Rebuilding the guard on resume is a free-money bug. It is not here."""
    checkpointer = MemoryCheckpointer()
    loop = AgentLoop(
        model=FakeModel(default=refund_script()),
        tools=world.tools(),
        checkpointer=checkpointer,
        cost_fn=lambda response: 10,
    )
    state = loop.step(loop.start("refund the damaged mug", run_id="run-1"))

    resumed = AgentLoop(
        model=FakeModel(default=refund_script()),
        tools=world.tools(),
        cost_fn=lambda response: 10,
    )
    resumed.resume(state)

    assert resumed.budget.spent_cents == 40


# ------------------------------------------------------------------ journal


def test_journal_records_models_and_effects(world: World) -> None:
    """The two sources of divergence are the two things recorded."""
    journal = MemoryJournal()
    runner = DurableRunner(
        model=FakeModel(default=refund_script()),
        tools=world.tools(),
        journal=journal,
    )
    runner.start("refund the damaged mug", run_id="run-1")
    types = [record["type"] for record in journal.records("run-1")]

    assert types[0] == "run.started"
    assert types.count("model.response") == 4
    assert types.count("tool.effect") == 3
    assert types[-1] == "run.finished"


def test_file_journal_survives_a_new_runner(
    tmp_path: Path, world: World
) -> None:
    """A journal on disk is what makes a four-hour human wait cheap."""
    path = tmp_path / "journal.jsonl"
    first = DurableRunner(
        model=FakeModel(default=refund_script()),
        tools=world.tools(),
        journal=FileJournal(path),
    )
    with pytest.raises(SimulatedCrash):
        first.start("refund the damaged mug", run_id="run-1", crash_after_step=2)

    second = DurableRunner(
        model=FakeModel(default=refund_script()),
        tools=world.tools(),
        journal=FileJournal(path),
    )
    state = second.resume("run-1")

    assert state.status == "succeeded"
    assert world.total_refunded_cents(DAMAGED_ORDER) == 3250
    assert world.call_count("issue_refund") == 1


def test_replay_touches_nothing(world: World) -> None:
    """Reconstructing what an agent did must not do any of it again."""
    runner = DurableRunner(
        model=FakeModel(default=refund_script()), tools=world.tools()
    )
    original = runner.start("refund the damaged mug", run_id="run-1")
    before = world.snapshot()

    replayed = runner.replay("run-1")

    assert world.snapshot() == before
    assert replayed.status == original.status
    assert replayed.step == original.step
    assert replayed.final_text == original.final_text


def test_replay_of_a_crashed_run_stops_where_it_stopped(
    world: World,
) -> None:
    """Forensics: the journal shows exactly how far the run got."""
    runner = DurableRunner(
        model=FakeModel(default=refund_script()), tools=world.tools()
    )
    with pytest.raises(SimulatedCrash):
        runner.start("refund the damaged mug", run_id="run-1", crash_after_step=2)

    replayed = runner.replay("run-1")

    assert replayed.step == 2
    assert replayed.status == "running"


def test_cancel_is_durable(world: World) -> None:
    """A kill switch that only works in memory is not a kill switch."""
    runner = DurableRunner(
        model=FakeModel(default=refund_script()), tools=world.tools()
    )
    with pytest.raises(SimulatedCrash):
        runner.start("refund the damaged mug", run_id="run-1", crash_after_step=1)

    runner.cancel("run-1", reason="customer withdrew the request")

    assert runner.status("run-1") == "cancelled"
    with pytest.raises(RunCancelled):
        runner.resume("run-1")
    assert world.total_refunded_cents(DAMAGED_ORDER) == 0


def test_human_wait_suspends_and_resumes(world: World) -> None:
    """A run waiting on a person holds a journal, not a process."""
    approvals = ApprovalStore()
    runner = DurableRunner(
        model=FakeModel(
            default=[
                ToolCall(
                    "c1",
                    "issue_refund",
                    {
                        "order_id": DELIVERED_ORDER,
                        "amount_cents": 8400,
                        "reason": "not_delivered",
                    },
                ),
                "I have refunded US$84.00 in full.",
            ]
        ),
        tools=world.tools(),
        policy=default_northstar_policy(),
        approvals=approvals,
        principal=Principal.of("CUST-8841", "refunds:write"),
    )
    suspended = runner.start("refund in full", run_id="run-1")

    assert suspended.status == "waiting_approval"
    assert world.total_refunded_cents(DELIVERED_ORDER) == 0
    assert [r["type"] for r in runner.history("run-1")][-1] == "run.suspended"

    request = approvals.pending()[0]
    resumed = runner.approve("run-1", request.id, by="ops@northstar")

    assert resumed.status == "succeeded"
    assert world.total_refunded_cents(DELIVERED_ORDER) == 8400
    assert world.call_count("issue_refund") == 1


def test_denied_approval_leaves_the_run_suspended(world: World) -> None:
    """A no does not become a yes just because the agent asked again."""
    approvals = ApprovalStore()
    runner = DurableRunner(
        model=FakeModel(
            default=[
                ToolCall(
                    "c1",
                    "issue_refund",
                    {
                        "order_id": DELIVERED_ORDER,
                        "amount_cents": 8400,
                        "reason": "changed_mind",
                    },
                ),
                "Refunded.",
            ]
        ),
        tools=world.tools(),
        policy=default_northstar_policy(),
        approvals=approvals,
        principal=Principal.of("CUST-8841", "refunds:write"),
    )
    runner.start("refund in full", run_id="run-1")
    request = approvals.pending()[0]
    state = runner.approve(
        "run-1", request.id, by="ops@northstar", approved=False
    )

    assert state.status == "waiting_approval"
    assert world.total_refunded_cents(DELIVERED_ORDER) == 0


def test_idempotency_is_on_by_default_for_durable_runs(
    world: World,
) -> None:
    """Replay skips re-execution; the key covers everything replay cannot."""
    runner = DurableRunner(
        model=FakeModel(default=refund_script()), tools=world.tools()
    )
    runner.start("refund the damaged mug", run_id="run-1")
    refunds = world.refunds_for(DAMAGED_ORDER)

    assert len(refunds) == 1
    assert refunds[0].idempotency_key is not None
