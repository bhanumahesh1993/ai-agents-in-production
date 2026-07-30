"""The Chapter 7 properties, as assertions on behaviour.

Every test here is about what the agent *did*, read off the world's refund
ledger or off the assembled message list. None of them assert on the
wording of a summary, because the summary is not the thing under test: the
naive compactor's summary is good, and the run still pays twice.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from budget import ContextBudget, account, fits  # noqa: E402
from compact import (  # noqa: E402
    KEEP_RECENT,
    Summarizer,
    align_boundary,
    compact,
    naive_compact,
)
from eval_compaction import BUDGET, measure, run_one  # noqa: E402
from history import make_get_run_history  # noqa: E402
from northstar_contracts import (  # noqa: E402
    EventLog,
    Message,
    RunState,
    World,
    event_record,
)
from pinned import WRITE_TOOLS, ledger_events, pinned_facts  # noqa: E402
from session import AMOUNT, ORDER, remembers_refund  # noqa: E402

LONG_TASK = 12
SHORT_TASK = 1


# ------------------------------------------------------- the headline result


def test_naive_compaction_pays_the_customer_twice() -> None:
    """The opening incident, reproduced by a good summariser."""
    outcome = run_one(LONG_TASK, "naive")

    assert outcome.error == ""
    assert len(outcome.world.refunds_for(ORDER)) == 2
    assert outcome.world.total_refunded_cents(ORDER) == AMOUNT * 2
    assert outcome.passed is False


def test_pinned_compaction_pays_once_at_the_same_ceiling() -> None:
    """Same budget, same summariser, one refund."""
    naive = run_one(LONG_TASK, "naive")
    pinned = run_one(LONG_TASK, "pinned")

    assert len(pinned.world.refunds_for(ORDER)) == 1
    assert pinned.world.total_refunded_cents(ORDER) == AMOUNT
    assert pinned.passed is True
    # The repair is not a smaller window. Both configurations compacted,
    # and both held the same ceiling.
    assert pinned.compactions > 0
    assert pinned.peak_tokens <= BUDGET.content_ceiling
    assert pinned.peak_tokens <= naive.peak_tokens


def test_uncompacted_runs_break_the_budget_the_others_hold() -> None:
    """What compaction buys, stated as a number rather than a claim."""
    none = run_one(LONG_TASK, "none")
    pinned = run_one(LONG_TASK, "pinned")

    assert none.error != ""            # exhausted its cost ceiling
    assert pinned.error == ""
    assert pinned.peak_tokens < none.peak_tokens


def test_short_sessions_hide_every_failure_in_this_chapter() -> None:
    """Why a six-turn test suite passes on a compactor that loses money."""
    for mode in ("none", "naive", "pinned"):
        outcome = run_one(SHORT_TASK, mode)
        assert outcome.passed is True
        assert len(outcome.world.refunds_for(ORDER)) == 1
    assert run_one(SHORT_TASK, "naive").compactions == 0


def test_measure_reports_a_rate_not_an_anecdote() -> None:
    """pass@1 separates the three configurations on the same task set."""
    pinned = measure(compaction=True, pinned=True)
    naive = measure(compaction=True, pinned=False)

    assert pinned.n == naive.n == 12
    assert pinned.pass_1 == 1.0
    assert naive.pass_1 < pinned.pass_1
    # Twelve runs is a small sample and the interval says so.
    low, high = naive.interval
    assert low < naive.pass_1 < high


# ------------------------------------------------------ the mechanism itself


def test_pinned_facts_are_computed_from_the_log_not_summarised() -> None:
    """No model is involved, and the write set comes from the registry."""
    log = EventLog()
    log.append(event_record("run-x", 4, "tool.called", {
        "call_id": "c9",
        "tool": "issue_refund",
        "arguments": {"order_id": ORDER, "amount_cents": AMOUNT},
    }))
    log.append(event_record("run-x", 4, "tool.result", {
        "call_id": "c9", "tool": "issue_refund", "ok": True,
    }))
    log.append(event_record("run-x", 5, "tool.called", {
        "call_id": "c10", "tool": "get_order", "arguments": {},
    }))
    log.append(event_record("run-x", 5, "tool.result", {
        "call_id": "c10", "tool": "get_order", "ok": True,
    }))

    facts = pinned_facts(ledger_events(log))

    assert len(facts) == 1                       # the read is not a fact
    assert str(AMOUNT) in facts[0]               # amount carried verbatim
    assert ORDER in facts[0]                     # identifier carried verbatim
    assert "issue_refund" in WRITE_TOOLS
    assert "get_order" not in WRITE_TOOLS


def test_compaction_is_idempotent() -> None:
    """The middleware runs before every call; twice must be a no-op."""
    state = RunState(run_id="run-x", messages=_bulky_messages(40))
    once = compact(state, BUDGET, Summarizer())
    twice = compact(RunState(run_id="run-x", messages=once), BUDGET,
                    Summarizer())

    assert len(once) < len(state.messages)
    assert twice == once


def test_the_boundary_never_splits_a_call_from_its_result() -> None:
    """The two-line fix missing from every first version of a compactor."""
    messages = [
        Message(role="system", content="prompt"),
        Message(role="user", content="goal"),
        Message(role="assistant", content=[
            {"type": "tool_use", "id": "c1", "name": "get_order", "input": {}}
        ]),
        Message(role="tool", content={"call_id": "c1", "tool": "get_order"}),
        Message(role="tool", content={"call_id": "c2", "tool": "get_policy"}),
        Message(role="assistant", content="done"),
    ]
    # Asking to split onto the second orphaned observation walks back to
    # the assistant message that requested it.
    assert align_boundary(messages, 4) == 2
    assert messages[align_boundary(messages, 4)].role == "assistant"
    # And the split never eats the system prompt or the goal.
    assert align_boundary(messages, 0) == 2


def test_exceeded_names_the_line_item_that_blew_up() -> None:
    """A boolean tells you a run tripped; names tell you what to fix."""
    budget = ContextBudget(total=1000, system=10, tools=10, pinned=10,
                           history=10, retrieved=10, reserve=100)
    used = {"system": 5, "tools": 400, "pinned": 5, "history": 5,
            "retrieved": 5}

    assert budget.exceeded(used) == ["tools"]
    assert budget.exceeded(dict.fromkeys(used, 0)) == []


def test_tool_definitions_are_counted_as_context() -> None:
    """The line item teams forget, and the one compaction cannot reach."""
    world = World()
    specs = world.tool_specs()
    messages = [Message(role="system", content="prompt")]

    with_tools = account(messages, specs)
    without = account(messages, [])

    assert with_tools["tools"] > 1000
    assert without["tools"] == 0
    tight = ContextBudget(total=100_000, tools=10, reserve=0)
    assert fits(messages, tight, []) is True
    assert fits(messages, tight, specs) is False


def test_summary_prose_does_not_count_as_remembering() -> None:
    """A paraphrase of a transcript is not a ledger, and code says so."""
    summary = Summarizer.summarise([
        Message(role="tool", content={
            "tool": "get_order", "content": {"order_id": ORDER}
        }),
        Message(role="tool", content={"tool": "get_policy", "content": {}}),
    ])
    prose_only = [Message(role="system", content=summary)]

    assert ORDER in summary                # the summary is genuinely good
    assert "partial refund" in summary     # and mentions a refund in prose
    assert remembers_refund(prose_only) is False

    receipt = [Message(role="tool", content={
        "tool": "issue_refund",
        "content": {"refund_id": "RFND-00001", "amount_cents": AMOUNT},
    })]
    assert remembers_refund(receipt) is True


def test_the_summary_carries_a_pointer_back_into_detail() -> None:
    """Compaction without a retrieval path is amnesia."""
    state = RunState(run_id="run-x", messages=_bulky_messages(40))
    blocks = naive_compact(state, BUDGET, Summarizer())
    header = blocks[0].content

    assert "get_run_history" in header
    assert "steps 0-" in header

    log = EventLog()
    for step in range(4):
        log.append(event_record("run-x", step, "tool.called", {
            "call_id": f"c{step}", "tool": "get_order", "arguments": {},
        }))
    _, get_run_history = make_get_run_history(log)
    page = get_run_history(from_step=1, to_step=2)

    assert page["total"] == 2
    assert [r["step"] for r in page["records"]] == [1, 2]


def test_the_derived_idempotency_key_does_not_prevent_this_duplicate() -> None:
    """Chapter 1's repair is fully deployed here, and does not apply.

    Both refunds carry a key derived from the run and the step. They are
    different steps, so the keys differ, so the refund service treats them
    as two intents -- correctly. The duplicate was chosen, not retried.
    """
    outcome = run_one(LONG_TASK, "naive")
    keys = {
        r.idempotency_key for r in outcome.world.refunds_for(ORDER)
    }

    assert len(outcome.world.refunds_for(ORDER)) == 2
    assert len(keys) == 2
    assert None not in keys


def _bulky_messages(count: int) -> list[Message]:
    """A message list long enough to force a compaction event."""
    messages = [
        Message(role="system", content="prompt"),
        Message(role="user", content="goal"),
    ]
    for index in range(count):
        messages.append(
            Message(role="assistant", content=[{
                "type": "tool_use",
                "id": f"c{index}",
                "name": "get_order",
                "input": {"order_id": ORDER},
            }])
        )
        messages.append(
            Message(role="tool", content={
                "call_id": f"c{index}",
                "tool": "get_order",
                "ok": True,
                "content": {"order_id": ORDER, "filler": "x" * 200},
            })
        )
    assert len(messages) > KEEP_RECENT
    return messages
