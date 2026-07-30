"""Spans, cost attribution, and redaction."""

from __future__ import annotations

import io

import pytest
from northstar_contracts import ToolCall, World
from northstar_runtime import AgentLoop, FakeModel
from northstar_telemetry import (
    CostLedger,
    ModelPrice,
    Redactor,
    instrument,
)

from conftest import DAMAGED_ORDER, refund_script


def _run(world: World, exporter: str = "memory", **kwargs: object):
    """Run the standard refund script with telemetry attached."""
    loop = AgentLoop(
        model=FakeModel(default=refund_script()), tools=world.tools()
    )
    telemetry = instrument(loop, exporter=exporter, **kwargs)  # type: ignore[arg-type]
    state = loop.run("refund the damaged mug", run_id="run-1")
    return telemetry, state


def test_one_agent_span_per_run(world: World) -> None:
    """The run is the top of the trace tree."""
    telemetry, _ = _run(world)
    agent_spans = telemetry.named("gen_ai.agent")

    assert len(agent_spans) == 1
    assert agent_spans[0].attributes["gen_ai.operation.name"] == "invoke_agent"
    assert agent_spans[0].attributes["northstar.status"] == "succeeded"
    assert agent_spans[0].attributes["northstar.tool_calls"] == 3


def test_model_and_tool_spans_carry_gen_ai_attributes(
    world: World,
) -> None:
    """Attribute names follow the gen_ai conventions, not house style."""
    telemetry, _ = _run(world)
    model_span = telemetry.named("gen_ai.model")[0]
    tool_span = telemetry.named("gen_ai.tool")[0]

    assert model_span.attributes["gen_ai.operation.name"] == "chat"
    assert model_span.attributes["gen_ai.request.model"] == "fake-model-1"
    assert model_span.attributes["gen_ai.usage.input_tokens"] > 0
    assert tool_span.attributes["gen_ai.operation.name"] == "execute_tool"
    assert tool_span.attributes["gen_ai.tool.name"] == "get_order"
    assert tool_span.attributes["gen_ai.tool.call.id"] == "c1"


def test_tool_span_duration_is_measured_not_guessed(
    world: World,
) -> None:
    """Tool spans are bracketed by two real events, so the time is real."""
    telemetry, _ = _run(world)

    for span in telemetry.named("gen_ai.tool"):
        assert span.end_time >= span.start_time


def test_failed_tool_marks_the_span_as_an_error(world: World) -> None:
    """A trace that shows every span green is a trace nobody will trust."""
    loop = AgentLoop(
        model=FakeModel(
            default=[
                ToolCall("c1", "get_order", {"order_id": "NR-0000-0000000"}),
                "I could not find it.",
            ]
        ),
        tools=world.tools(),
    )
    telemetry = instrument(loop, exporter="memory")
    loop.run("look up a bad id", run_id="run-1")
    tool_span = telemetry.named("gen_ai.tool")[0]

    assert tool_span.status == "error"
    assert "no order" in tool_span.attributes["northstar.error"]


def test_arguments_are_redacted_before_export(world: World) -> None:
    """Traces leave your trust boundary. Redact at the boundary."""
    loop = AgentLoop(
        model=FakeModel(
            default=[
                ToolCall(
                    "c1",
                    "send_message",
                    {
                        "order_id": DAMAGED_ORDER,
                        "body": "We refunded ada@example.com US$32.50.",
                    },
                ),
                "Message sent.",
            ]
        ),
        tools=world.tools(),
    )
    telemetry = instrument(loop, exporter="memory")
    loop.run("apologise", run_id="run-1")
    arguments = telemetry.named("gen_ai.tool")[0].attributes[
        "northstar.tool.arguments"
    ]

    assert arguments["order_id"] == DAMAGED_ORDER
    assert arguments["body"] == "[redacted]"


def test_console_exporter_writes_one_line_per_span(world: World) -> None:
    """The default exporter needs nothing installed and prints something."""
    stream = io.StringIO()
    telemetry, _ = _run(world, exporter="console", stream=stream)
    lines = [line for line in stream.getvalue().splitlines() if line]

    assert len(lines) == len(telemetry.spans)
    assert any(line.startswith("[gen_ai.agent]") for line in lines)


def test_unknown_exporter_is_rejected(world: World) -> None:
    """Fail on a typo rather than silently dropping every span."""
    loop = AgentLoop(model=FakeModel(default=["done"]), tools=world.tools())

    with pytest.raises(ValueError, match="unknown exporter"):
        instrument(loop, exporter="datadog")


def test_importing_telemetry_does_not_require_opentelemetry() -> None:
    """The whole package must import on a machine with no OTel at all."""
    import northstar_telemetry  # noqa: PLC0415

    assert "opentelemetry" not in repr(northstar_telemetry.instrument)


# -------------------------------------------------------------------- cost


def test_mock_mode_costs_nothing(world: World) -> None:
    """A repository that charges you to run its tests will not be run."""
    telemetry, _ = _run(world)

    assert telemetry.ledger.per_run_cents("run-1") == 0


def test_cost_ledger_attributes_spend_per_run_and_model() -> None:
    """Cost per run is the number a dashboard needs; per model is the lever."""
    ledger = CostLedger()
    ledger.register("model-a", ModelPrice(300, 1500, note="illustrative"))
    ledger.register("model-b", ModelPrice(30, 150, note="illustrative"))
    ledger.record("model-a", 1_000_000, 100_000, run_id="run-1")
    ledger.record("model-b", 1_000_000, 100_000, run_id="run-2")

    assert ledger.per_run_cents("run-1") == 450
    assert ledger.per_run_cents("run-2") == 45
    assert ledger.total_cents() == 495
    assert list(ledger.by_model()) == ["model-a", "model-b"]


def test_cost_arithmetic_does_not_drift() -> None:
    """Integer nanocents in, one rounding at the edge, no float drift."""
    ledger = CostLedger()
    ledger.register("m", ModelPrice(1, 1, note="illustrative"))
    for _ in range(1000):
        ledger.record("m", 1, 1, run_id="run-1")

    # 2000 tokens at 1 cent per million is 0.002 cents, which rounds up to
    # one cent once, not to 1000 cents.
    assert ledger.per_run_cents("run-1") == 1


def test_strict_ledger_refuses_to_guess_a_price() -> None:
    """Pricing an unrecognised model at a guessed rate makes it fiction."""
    ledger = CostLedger(strict=True)

    with pytest.raises(KeyError, match="no price for model"):
        ledger.record("some-new-model", 100, 100)


def test_report_flags_illustrative_prices() -> None:
    """The default table is a placeholder and says so in the output."""
    assert CostLedger().report()["prices_are_illustrative"] is True


# --------------------------------------------------------------- redaction


def test_redactor_removes_known_fields() -> None:
    """Field names first: they are deterministic."""
    redactor = Redactor.default()
    out = redactor.redact(
        {"order_id": "NR-1", "email": "ada@example.com", "amount_cents": 8400}
    )

    assert out == {
        "order_id": "NR-1",
        "email": "[redacted]",
        "amount_cents": 8400,
    }


def test_redactor_catches_patterns_in_free_text() -> None:
    """Patterns second: for what ends up in a free-text field anyway."""
    redactor = Redactor.default()
    out = redactor.redact({"note": "reply to ada@example.com about it"})

    assert out["note"] == "reply to [redacted:email] about it"


def test_redactor_can_hash_for_correlation() -> None:
    """Count distinct customers without learning who they are."""
    redactor = Redactor.default(hash_values=True)
    first = redactor.redact({"email": "ada@example.com"})["email"]
    second = redactor.redact({"email": "ada@example.com"})["email"]
    other = redactor.redact({"email": "grace@example.com"})["email"]

    assert first == second
    assert first != other
    assert first.startswith("redacted:")


def test_redactor_leaves_the_event_envelope_alone() -> None:
    """You still have to be able to find the record you are looking for."""
    redactor = Redactor.default()
    out = redactor.redact_event(
        {
            "run_id": "run-1",
            "step": 2,
            "type": "tool.called",
            "ts": 1.0,
            "payload": {"arguments": {"body": "hello"}},
        }
    )

    assert out["run_id"] == "run-1"
    assert out["step"] == 2
    assert out["payload"]["arguments"]["body"] == "[redacted]"


def test_redactor_does_not_mutate_its_input() -> None:
    """Redaction returns a copy. The run still needs the real values."""
    redactor = Redactor.default()
    original = {"email": "ada@example.com"}
    redactor.redact(original)

    assert original["email"] == "ada@example.com"


def test_redaction_survives_nesting() -> None:
    """Sensitive fields hide inside lists of dicts, so walk the whole thing."""
    redactor = Redactor.default()
    out = redactor.redact(
        {"messages": [{"body": "secret"}, {"body": "also secret"}]}
    )

    assert [m["body"] for m in out["messages"]] == [
        "[redacted]",
        "[redacted]",
    ]
