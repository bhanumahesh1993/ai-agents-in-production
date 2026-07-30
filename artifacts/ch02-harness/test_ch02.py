"""The Chapter 2 properties, as assertions.

The demo prints; this fails a build. Every assertion here is about behaviour:
what is in the refund ledger, what the guard raises, which axis has no
enforcement point. None of them reads a message the agent wrote about itself.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import checkpoint_wrong
import demo
import pytest
from autonomy import (
    AXES,
    AutonomyBudget,
    AutonomyPolicy,
    Wiring,
    guard_for,
    load_budget,
    parse_yaml,
    unenforced,
)
from budget import BudgetExceeded, BudgetGuard
from checkpoint import SqliteCheckpointer, config_hash_for, decode, encode
from journal import StepJournal
from loop import WorkerKilled
from northstar_contracts import (
    Message,
    RunState,
    ToolCall,
    ToolSpec,
    World,
    idempotency_key,
)
from northstar_policy import Decision, Principal
from refund_ledger import RefundLedger
from registry import HarnessRegistry
from runner import ConfigDrift, UnknownRun
from suitability import ADDRESS_CHANGE, REFUND_TRIAGE, assess

# -- the property the chapter exists for -------------------------------------


def _kill_a_refund_run(db: Path) -> int:
    """Start a run that dies the instant the refund lands.

    Returns:
        Refund rows the service holds at the moment the worker died.
    """
    with demo.wire(db, inject_timeout=True, kill_on_refund=True) as worker:
        with pytest.raises(WorkerKilled):
            worker.runner.start(demo.GOAL, demo.RUN_ID)
        return len(worker.service.rows(demo.ORDER))


def test_a_run_killed_mid_refund_resumes_to_one_refund(tmp_path: Path) -> None:
    """The chapter's claim: at-least-once plus a derived key is enough."""
    db = tmp_path / "run.sqlite"
    assert _kill_a_refund_run(db) == 1          # the write landed

    # Nothing of the first worker survives: new world, new registry, new
    # loop, new connections. Only the file is shared.
    with demo.wire(db, inject_timeout=False, kill_on_refund=False) as worker:
        state = worker.runner.resume(demo.RUN_ID)

        assert state.status == "succeeded"
        assert worker.runner.settled == ["issue_refund"]    # it re-dispatched
        assert len(worker.service.rows(demo.ORDER)) == 1     # and paid once
        assert worker.service.total_cents(demo.ORDER) == demo.AMOUNT


def test_the_resumed_worker_can_be_a_different_process(tmp_path: Path) -> None:
    """A second interpreter, sharing only a file, finishes the run."""
    db = tmp_path / "run.sqlite"
    _kill_a_refund_run(db)

    child = subprocess.run(
        [sys.executable, str(Path(demo.__file__).resolve()), "--resume", str(db)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert child.returncode == 0, child.stderr
    assert "status=succeeded" in child.stdout
    assert "ledger_rows=1" in child.stdout

    service = RefundLedger(World(), db)
    assert service.total_cents(demo.ORDER) == demo.AMOUNT
    service.close()


def test_the_key_is_derived_so_two_processes_compute_the_same_one() -> None:
    """No shared memory required, which is the whole point of deriving it."""
    call = ToolCall("c3", "issue_refund", {})
    tools = HarnessRegistry().register_all(World().tools())
    stamped = tools.stamp(call, demo.RUN_ID, "2:c3")
    assert stamped.arguments["idempotency_key"] == idempotency_key(
        demo.RUN_ID, "2:c3"
    )
    # A read is not stamped: there is nothing to make safe.
    read = tools.stamp(ToolCall("c1", "get_order", {}), demo.RUN_ID, "1:c1")
    assert "idempotency_key" not in read.arguments


def test_the_wrong_checkpoint_boundary_pays_twice(tmp_path: Path) -> None:
    """Same fault, same kill, one ordering choice different."""
    db = tmp_path / "wrong.sqlite"
    with demo.wire(
        db, inject_timeout=True, kill_on_refund=True, unsafe=True
    ) as first:
        with pytest.raises(WorkerKilled):
            first.runner.start(demo.GOAL, demo.RUN_ID)

    with demo.wire(
        db, inject_timeout=False, kill_on_refund=False, unsafe=True
    ) as second:
        state = checkpoint_wrong.resume_from_history(
            second.runner.loop, second.runner.checkpointer, demo.RUN_ID
        )

        # The run is happy. The ledger is not: Chapter 1 by a new route.
        assert state.status == "succeeded"
        assert len(second.service.rows(demo.ORDER)) == 2
        assert second.service.total_cents(demo.ORDER) == demo.AMOUNT * 2


def test_a_resume_refuses_a_changed_configuration(tmp_path: Path) -> None:
    """ConfigDrift and UnknownRun are different operator problems."""
    db = tmp_path / "drift.sqlite"
    with demo.wire(db, inject_timeout=False, kill_on_refund=False) as first:
        first.runner.start(demo.GOAL, demo.RUN_ID)

    with demo.wire(db, inject_timeout=False, kill_on_refund=False) as second:
        second.runner.config_hash = config_hash_for(
            model="fake-model-1",
            system_prompt="You are a different agent.",
            specs=second.runner.tools.specs(),
        )
        with pytest.raises(ConfigDrift):
            second.runner.resume(demo.RUN_ID)
        with pytest.raises(UnknownRun):
            second.runner.resume("run_that_never_ran")


# -- the checkpointer --------------------------------------------------------


def test_a_stale_worker_cannot_overwrite_a_newer_checkpoint() -> None:
    """The one line of SQL: WHERE excluded.step >= checkpoints.step."""
    cp = SqliteCheckpointer(":memory:", config_hash="h")
    cp.save(RunState(run_id="run-1", step=6, budget_spent_cents=40))
    cp.save(RunState(run_id="run-1", step=2, budget_spent_cents=9))

    loaded = cp.load("run-1")
    assert loaded is not None
    assert loaded.step == 6
    assert loaded.budget_spent_cents == 40      # not the stale worker's view
    cp.close()


def test_a_checkpoint_round_trips_a_tool_result() -> None:
    """A checkpoint you cannot write to disk is not a checkpoint."""
    world = World()
    tools = HarnessRegistry().register_all(world.tools())
    result = tools.dispatch(
        ToolCall("c1", "get_order", {"order_id": demo.ORDER})
    )
    messages = [Message(role="tool", content=result)]
    restored = decode(encode(messages))
    assert restored[0].content.ok is True
    assert restored[0].content.content["total_cents"] == 8400


# -- the budget --------------------------------------------------------------


def test_every_limit_raises_rather_than_returning_success() -> None:
    """Exhaustion is a terminal status of its own, not a variety of success."""
    guard = BudgetGuard(max_turns=3, budget_cents=200, max_repeats=2).start()

    with pytest.raises(BudgetExceeded, match="max_turns=3"):
        guard.check(RunState(run_id="r", step=3))
    with pytest.raises(BudgetExceeded, match="budget_cents=200"):
        guard.check(RunState(run_id="r", step=1, budget_spent_cents=200))
    with pytest.raises(BudgetExceeded, match="deadline"):
        BudgetGuard(deadline_s=0.0).start().check(RunState(run_id="r"))


def test_the_no_progress_detector_catches_a_repeated_call() -> None:
    """Three budgets can all look healthy while nothing is happening."""
    call = {"type": "tool_use", "id": "c1", "name": "get_policy", "input": {}}
    state = RunState(run_id="r", step=1)
    for _ in range(3):
        state = state.with_messages(
            Message(role="assistant", content=[dict(call)])
        )
    guard = BudgetGuard(max_repeats=3).start()
    assert guard.repeated_calls(state) == 3
    with pytest.raises(BudgetExceeded, match="no progress"):
        guard.check(state)


def test_a_deadline_does_not_burn_while_a_run_waits_for_a_human() -> None:
    """Which clock counts is a policy decision, and this is the choice."""
    clock = iter([0.0, 0.0, 100.0, 100.0])
    journal = StepJournal("run-1")
    journal.append("run.suspended", {}, at=10.0)
    journal.append("run.resumed", {}, at=95.0)

    guard = BudgetGuard(
        deadline_s=30.0, journal=journal, clock=lambda: next(clock)
    ).start()
    state = RunState(run_id="run-1", step=1)

    # 100 seconds elapsed, 85 of them spent suspended: 15 seconds of work.
    assert guard.active_seconds(state) == pytest.approx(15.0)
    guard.check(state)      # does not raise


# -- dispatch ----------------------------------------------------------------


def test_dispatch_never_raises_and_always_answers_the_call() -> None:
    """Every tool_use block gets exactly one result, including the failures."""
    world = World()
    tools = HarnessRegistry().register_all(world.tools())

    unknown = tools.dispatch(ToolCall("c1", "refund_everything", {}))
    assert unknown.ok is False
    assert unknown.call_id == "c1"
    assert "Available tools" in str(unknown.content["error"])
    assert unknown.retryable is False

    coerced = tools.dispatch(
        ToolCall("c2", "issue_refund", {"order_id": demo.ORDER,
                                        "amount_cents": "32.50",
                                        "reason": "damaged"})
    )
    assert coerced.ok is False              # rejected, not coerced to 3250
    assert "amount_cents" in str(coerced.content["error"])
    assert world.total_refunded_cents(demo.ORDER) == 0

    world.inject_fault("get_order", kind="timeout")
    timed_out = tools.dispatch(
        ToolCall("c3", "get_order", {"order_id": demo.ORDER})
    )
    assert timed_out.ok is False
    assert timed_out.retryable is True      # set by code, from the class


def test_a_policy_denial_comes_back_as_a_result() -> None:
    """Dropping the call would leave a malformed conversation."""
    world = World()
    budget = load_budget()
    policy = AutonomyPolicy(budget, "CUST-8841", demo.owners(world))
    tools = HarnessRegistry(policy=policy).register_all(world.tools())

    foreign = tools.dispatch(
        ToolCall("c1", "get_order", {"order_id": "NR-2026-0042110"})
    )
    assert foreign.ok is False
    assert foreign.call_id == "c1"           # answered, not omitted
    assert foreign.content["decision"] == "deny"
    assert tools.denials and tools.denials[0].name == "get_order"


def test_a_result_over_its_budget_is_flagged_in_the_body() -> None:
    """A truncation the model cannot see produces a confident wrong answer."""
    world = World()
    tools = HarnessRegistry().register_all(world.tools())
    tiny = ToolSpec(
        name="get_order_tiny",
        description="the same read, with a 10-token budget",
        input_schema=world.tool_specs()[0].input_schema,
        output_schema={"type": "object"},
        writes=False,
        idempotent=True,
        max_result_tokens=10,
    )
    tools.register(tiny, world.get_order)
    result = tools.dispatch(
        ToolCall("c1", "get_order_tiny", {"order_id": demo.ORDER})
    )
    assert result.truncated is True
    assert result.content["truncated"] is True      # visible to the model
    assert "truncation_note" in result.content


# -- the worksheet -----------------------------------------------------------


def _wiring() -> Wiring:
    world = World()
    budget = load_budget()
    policy = AutonomyPolicy(budget, demo.CUSTOMER, demo.owners(world))
    tools = HarnessRegistry(policy=policy).register_all(world.tools())
    return Wiring(guard_for(budget), tools, policy)


def test_every_axis_in_the_file_is_read_by_a_live_component() -> None:
    """A number written twice is not a control."""
    assert unenforced(load_budget(), _wiring()) == []


def test_an_unset_axis_fails_rather_than_defaulting_to_unlimited() -> None:
    """Seven of Northstar's eight axes were unset on the day."""
    wiring = _wiring()
    for axis in AXES:
        raw = dict(load_budget().raw)
        del raw[axis]
        problems = unenforced(AutonomyBudget(raw), wiring)
        assert any(axis in p for p in problems), f"{axis} passed while unset"


def test_a_number_changed_in_the_file_alone_is_caught() -> None:
    """The guard has to be holding the number, not agreeing with it."""
    raw = load_budget().raw
    raw["step_budget"] = {**raw["step_budget"], "max_turns": 99}
    problems = unenforced(AutonomyBudget(raw), _wiring())
    assert problems == ["step_budget: no enforcement point"]


def test_the_policy_gates_a_refund_at_the_threshold_not_above_it() -> None:
    """A threshold you can sit exactly on is one someone will sit on."""
    world = World()
    budget = load_budget()
    policy = AutonomyPolicy(budget, "CUST-8841", demo.owners(world))
    args = {"order_id": demo.ORDER, "reason": "damaged"}

    at = ToolCall("c1", "issue_refund", {**args, "amount_cents": 5000})
    under = ToolCall("c2", "issue_refund", {**args, "amount_cents": 4999})
    assert policy.evaluate(Principal(), at, {}) is Decision.REQUIRE_APPROVAL
    assert policy.evaluate(Principal(), under, {}) is Decision.ALLOW


def test_the_yaml_subset_parser_reads_the_shipped_file() -> None:
    """No YAML dependency, so the parser is part of the artifact."""
    parsed = parse_yaml(
        "agent: x\nrisk_tier: 2\na: [p, q]\nb: {c: 1}\nd:\n  e: 3   # note\n"
    )
    assert parsed == {
        "agent": "x",
        "risk_tier": 2,
        "a": ["p", "q"],
        "b": {"c": 1},
        "d": {"e": 3},
    }
    budget = load_budget()
    assert budget.agent == "northstar-support"
    assert budget.transaction_cents_per_action == 5000
    assert budget.max_turns == 12


# -- the gate ----------------------------------------------------------------


def test_the_suitability_gate_fails_closed() -> None:
    """An unanswered worksheet is not an approval."""
    assert assess(REFUND_TRIAGE).build_an_agent is True
    assert assess(ADDRESS_CHANGE).build_an_agent is False
    empty = assess({})
    assert empty.build_an_agent is False
    assert len(empty.unanswered) == 6
    assert all("build " in line for line in empty.report())


def test_the_gate_needs_all_six_not_most_of_them() -> None:
    """Four out of six is not a decision anyone can act on."""
    for key in REFUND_TRIAGE:
        answers = {**REFUND_TRIAGE, key: False}
        verdict = assess(answers)
        assert verdict.build_an_agent is False
        assert [c.key for c in verdict.unmet] == [key]
