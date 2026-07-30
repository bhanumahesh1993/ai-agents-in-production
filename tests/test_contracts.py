"""Contracts: money, canonical JSON, idempotency keys, events, world."""

from __future__ import annotations

import pytest
from northstar_contracts import (
    EventLog,
    Message,
    RunState,
    ToolCall,
    ToolResult,
    ToolTimeout,
    World,
    canonical_json,
    event_record,
    idempotency_key,
)
from northstar_contracts.errors import RetryableToolError, ToolValidationError

from conftest import DAMAGED_ORDER, DELIVERED_ORDER, FLAGGED_ORDER


def test_idempotency_key_is_stable_and_32_hex_chars() -> None:
    """The same run and step always produce the same key."""
    key = idempotency_key("run-1", 3)

    assert key == idempotency_key("run-1", 3)
    assert key != idempotency_key("run-1", 4)
    assert key != idempotency_key("run-2", 3)
    assert len(key) == 32
    assert all(c in "0123456789abcdef" for c in key)


def test_canonical_json_ignores_key_order() -> None:
    """Two structurally equal payloads serialise identically."""
    a = {"amount_cents": 8400, "order_id": DELIVERED_ORDER}
    b = {"order_id": DELIVERED_ORDER, "amount_cents": 8400}

    assert canonical_json(a) == canonical_json(b)


def test_canonical_json_refuses_what_it_cannot_encode() -> None:
    """A payload you cannot serialise is one you cannot fingerprint."""
    with pytest.raises(TypeError):
        canonical_json({"fn": lambda: None})


def test_event_record_rejects_unknown_types() -> None:
    """The event vocabulary is closed, so dashboards can rely on it."""
    with pytest.raises(ValueError, match="unknown event type"):
        event_record("run-1", 0, "run.exploded")


def test_event_log_round_trips_through_jsonl() -> None:
    """A log written to disk reads back identically."""
    log = EventLog()
    log.emit("run-1", 0, "run.started", {"goal": "refund"}, ts=1.0)
    log.emit("run-1", 1, "tool.called", {"tool": "get_order"}, ts=2.0)

    restored = EventLog.from_jsonl(log.to_jsonl())

    assert restored.records == log.records
    assert len(restored.of_type("tool.called")) == 1


def test_run_state_round_trips_and_stays_immutable() -> None:
    """A checkpoint payload survives the trip and does not alias."""
    state = RunState(run_id="run-1", messages=[Message("user", "hello")])
    later = state.with_messages(Message("assistant", "hi")).advance(
        spent_cents=7
    )

    assert len(state.messages) == 1  # the original is untouched
    assert later.step == 1
    assert later.budget_spent_cents == 7
    assert RunState.from_dict(later.to_dict()) == later


def test_tool_result_failure_shape() -> None:
    """Error results carry the two fields the model reasons about."""
    result = ToolResult.failure("c1", "gateway timeout", retryable=True)

    assert result.content == {
        "error": "gateway timeout",
        "retryable": True,
    }
    assert result.retryable
    assert result.error == "gateway timeout"


def test_message_exposes_tool_calls_from_content_blocks() -> None:
    """Trajectories are recovered from the transcript, not a side channel."""
    message = Message(
        role="assistant",
        content=[
            {"type": "text", "text": "Checking."},
            {
                "type": "tool_use",
                "id": "c1",
                "name": "get_order",
                "input": {"order_id": DELIVERED_ORDER},
            },
        ],
    )

    assert message.tool_calls == [
        ToolCall("c1", "get_order", {"order_id": DELIVERED_ORDER})
    ]


# --------------------------------------------------------------- the world


def test_fixtures_match_the_book(world: World) -> None:
    """The three orders every chapter refers to."""
    assert world.get_order(DELIVERED_ORDER)["total_cents"] == 8400
    assert len(world.get_order(DELIVERED_ORDER)["items"]) == 2
    assert world.get_order(DELIVERED_ORDER)["status"] == "delivered"
    assert world.get_order(DAMAGED_ORDER)["total_cents"] == 3250
    assert "damaged_on_arrival" in world.get_order(DAMAGED_ORDER)["flags"]
    assert world.get_order(FLAGGED_ORDER)["total_cents"] == 24000
    assert "fraud_review" in world.get_order(FLAGGED_ORDER)["flags"]


def test_policy_reports_the_approval_threshold(world: World) -> None:
    """5000 cents is the line between autonomy and a human."""
    policy = world.get_policy(reason="damaged")

    assert policy["approval_threshold_cents"] == 5000
    assert policy["rules"][0]["reason"] == "damaged"


def test_search_is_paginated_and_token_budgeted(world: World) -> None:
    """Search never returns more than the caller's budget allows."""
    page = world.search_orders(page=1, page_size=2)

    assert len(page["results"]) == 2
    assert page["next_page"] == 2
    assert page["total_matches"] == 3

    tiny = world.search_orders(page=1, page_size=3, max_result_tokens=10)

    assert tiny["truncated"]
    assert len(tiny["results"]) < 3


def test_refund_cannot_exceed_the_order_value(world: World) -> None:
    """The store enforces its own invariant. The agent is not trusted with it."""
    with pytest.raises(ToolValidationError, match="exceed the order value"):
        world.issue_refund(DAMAGED_ORDER, 9999, "damaged")


def test_refund_rejects_float_and_boolean_amounts(world: World) -> None:
    """Money is an integer number of cents. ``True`` is not 1 cent."""
    with pytest.raises(ToolValidationError):
        world.issue_refund(DAMAGED_ORDER, 32.50, "damaged")  # type: ignore[arg-type]
    with pytest.raises(ToolValidationError):
        world.issue_refund(DAMAGED_ORDER, True, "damaged")  # type: ignore[arg-type]


def test_timeout_fault_lands_the_write_before_it_raises(
    world: World,
) -> None:
    """The Chapter 1 fault, at its smallest: the effect outlives the error."""
    world.inject_fault("issue_refund", kind="timeout")

    with pytest.raises(ToolTimeout):
        world.issue_refund(DAMAGED_ORDER, 3250, "damaged")

    assert world.total_refunded_cents(DAMAGED_ORDER) == 3250


def test_error_fault_lands_nothing(world: World) -> None:
    """The safe failure: nothing happened, so a retry is free."""
    world.inject_fault("issue_refund", kind="error")

    with pytest.raises(RetryableToolError):
        world.issue_refund(DAMAGED_ORDER, 3250, "damaged")

    assert world.total_refunded_cents(DAMAGED_ORDER) == 0


def test_escalation_is_idempotent_by_design(world: World) -> None:
    """Some tools need a key. Some just need to be designed properly."""
    first = world.escalate_to_specialist(FLAGGED_ORDER, "possible fraud")
    second = world.escalate_to_specialist(FLAGGED_ORDER, "possible fraud")

    assert second["duplicate"] is True
    assert second["case_id"] == first["case_id"]
    assert len(world.escalations) == 1


def test_send_message_deduplicates_on_key(world: World) -> None:
    """A duplicate apology is worse than a duplicate refund. It is unsendable."""
    world.send_message(DAMAGED_ORDER, "Sorry.", idempotency_key="k1")
    again = world.send_message(DAMAGED_ORDER, "Sorry.", idempotency_key="k1")

    assert again["duplicate"] is True
    assert len(world.messages) == 1


def test_ledger_records_every_effect_that_landed(world: World) -> None:
    """The audit trail is the ledger, not the transcript."""
    world.issue_refund(DAMAGED_ORDER, 3250, "damaged")
    world.send_message(DAMAGED_ORDER, "Refunded.")

    kinds = [entry["kind"] for entry in world.ledger]

    assert kinds == ["refund_issued", "message_sent"]


def test_unknown_fault_kind_is_rejected_early(world: World) -> None:
    """Typos in test setup should fail loudly, not silently do nothing."""
    with pytest.raises(ValueError, match="unknown fault kind"):
        world.inject_fault("issue_refund", kind="explode")
