"""What the spans have to answer, as assertions.

Every test here reads the emitted spans or the cost ledger. None of them
asserts on a rendered string, because the point of a mapping layer is that the
spelling is allowed to change: a convention rename should break
:data:`spans.CONVENTION` and nothing else.

The load-bearing pair is
``test_breaking_propagation_loses_the_owner_not_the_money`` and
``test_completeness_drops_when_the_edge_is_missing``. Together they are
Northstar's April incident: the traces stayed complete, well-formed, exported,
retained, and green, and the question finance asked was unanswerable anyway.
"""

from __future__ import annotations

import pytest
from cost import PRICING_VERSION, CostLedger, Price, cached_split
from instrument import (
    AGENT_VERSION,
    CONFIG_HASH,
    SideEffectIndex,
    build_context,
    chosen_exporter,
)
from northstar_contracts import World
from northstar_telemetry import Redactor as BaseRedactor
from redaction import REDACTOR, Redactor
from spans import (
    CONVENTION,
    IDENTITY_ATTRIBUTES,
    REQUIRED_ATTRIBUTES,
    SPAN_NAMES,
    missing_required,
    required_for,
    tool_span_attributes,
)
from tickets import SUPPORT_PRINCIPAL, TICKETS, run_suite


def spans_named(suite, name: str) -> list:  # noqa: ANN001
    """Every span of one kind, across every run in a suite."""
    return [
        span
        for run in suite.runs
        for span in run.spans
        if span.name == name
    ]


# --------------------------------------------------- it works without OTel


def test_the_default_exporter_needs_nothing_installed(
    unset_exporter: None,
) -> None:
    """Offline is the default, not a fallback somebody has to remember."""
    assert chosen_exporter() == "console"


def test_the_suite_runs_with_every_shim_exporter() -> None:
    """``memory``, ``console``, and ``none`` all work with no OTel present."""
    for exporter in ("memory", "none"):
        suite = run_suite(
            propagate=True, exporter=exporter, tickets=TICKETS[:1]
        )
        assert len(suite.runs) == 1
        assert suite.runs[0].status == "succeeded"


def test_an_unknown_exporter_is_refused_rather_than_guessed() -> None:
    """Silently falling back to console would hide a broken deployment."""
    with pytest.raises(ValueError, match="unknown exporter"):
        run_suite(propagate=True, exporter="datadog", tickets=TICKETS[:1])


# ------------------------------------------------------- the span hierarchy


def test_the_run_is_the_trace_root_and_the_session_is_an_attribute(
    propagated,  # noqa: ANN001
) -> None:
    """A session as a root produces a trace no backend renders."""
    for run in propagated.runs:
        roots = [s for s in run.spans if s.name == SPAN_NAMES["run"]]
        assert roots, f"{run.run_id} emitted no run span"
        for span in run.spans:
            assert span.attributes["northstar.session.id"]
            assert span.name != "gen_ai.session"


def test_an_escalated_run_is_one_trace_with_a_handoff_in_it(
    propagated,  # noqa: ANN001
) -> None:
    """The eleven-line fix, as a property."""
    escalated = next(r for r in propagated.runs if r.escalated)
    assert len(escalated.trace_ids) == 1

    handoffs = [
        s for s in escalated.spans if s.name == SPAN_NAMES["handoff"]
    ]
    assert len(handoffs) == 1
    attributes = handoffs[0].attributes
    assert attributes["northstar.handoff.propagated"] is True
    assert attributes["northstar.handoff.child_trace_id"] == attributes[
        "northstar.trace.id"
    ]
    # The budget handed over is visible, so a delegation is not a retry.
    assert attributes["northstar.handoff.budget_cents"] > 0


def test_the_child_agent_span_carries_its_own_version(
    propagated,  # noqa: ANN001
) -> None:
    """One trace, two agent versions, and you can tell which is which."""
    escalated = next(r for r in propagated.runs if r.escalated)
    versions = {
        s.attributes["northstar.agent.version"] for s in escalated.spans
    }
    assert len(versions) == 2
    assert AGENT_VERSION in versions


# ------------------------------------------------- the seven, and the SLI


def test_every_tool_span_carries_all_seven(propagated) -> None:  # noqa: ANN001
    """Identity, authority, and consequence, on the span that had them."""
    tool_spans = spans_named(propagated, SPAN_NAMES["tool"])
    assert tool_spans
    for span in tool_spans:
        assert missing_required(span.attributes, span.name) == []


def test_a_model_span_is_not_asked_for_an_argument_digest() -> None:
    """A required field that is routinely empty is one nobody believes."""
    assert required_for(SPAN_NAMES["tool"]) == REQUIRED_ATTRIBUTES
    assert required_for(SPAN_NAMES["model"]) == IDENTITY_ATTRIBUTES
    assert len(REQUIRED_ATTRIBUTES) == 7


def test_every_write_span_carries_a_side_effect_identifier(
    propagated,  # noqa: ANN001
) -> None:
    """The join key between a span and the ledger row it claims to have made."""
    writes = [
        s
        for s in spans_named(propagated, SPAN_NAMES["tool"])
        if s.attributes["northstar.tool.writes"]
    ]
    assert writes
    for span in writes:
        assert span.attributes["northstar.side_effect.id"]
    assert all(not r.writes_without_receipt for r in propagated.runs)


def test_the_receipt_on_the_span_matches_the_ledger_row(
    propagated,  # noqa: ANN001
) -> None:
    """Reconciliation against authoritative state, as an assertion."""
    refunding = next(
        r for r in propagated.runs if r.ticket.expect_refund_cents == 3250
    )
    receipts = {
        s.attributes["northstar.side_effect.id"]
        for s in refunding.spans
        if s.name == SPAN_NAMES["tool"]
        and s.attributes["gen_ai.tool.name"].startswith("issue_refund")
    }
    ledger_ids = {
        row["refund_id"]
        for row in refunding.world.effects("refund_issued")
    }
    assert receipts
    assert receipts <= ledger_ids


def test_budget_remaining_falls_as_the_run_proceeds(
    propagated,  # noqa: ANN001
) -> None:
    """The attribute teams skip and then wish for."""
    longest = max(propagated.runs, key=lambda r: len(r.spans))
    readings = [
        s.attributes["northstar.budget.remaining_cents"]
        for s in longest.spans
        if s.name == SPAN_NAMES["model"]
    ]
    assert readings == sorted(readings, reverse=True)
    assert readings[0] > readings[-1]


def test_completeness_drops_when_the_edge_is_missing(
    propagated,  # noqa: ANN001
    broken,  # noqa: ANN001
) -> None:
    """Trace completeness is an SLI because it can be measured and it moves."""
    assert propagated.completeness() == 1.0
    assert broken.completeness() < propagated.completeness()

    incomplete = [r for r in broken.runs if not r.complete]
    assert len(incomplete) == 1
    assert incomplete[0].escalated is True
    # It is not missing attributes. It is missing an edge, which is worse,
    # because every span validates on its own.
    assert incomplete[0].missing_attributes == []
    assert len(incomplete[0].trace_ids) == 2


# ------------------------------------------------------------- attribution


def test_breaking_propagation_loses_the_owner_not_the_money(
    propagated,  # noqa: ANN001
    broken,  # noqa: ANN001
) -> None:
    """April, exactly: identical spend, and a fifth of it belonging to nobody."""
    assert propagated.total_exact_cents() == broken.total_exact_cents()
    assert propagated.unattributed_share() == 0.0
    assert broken.unattributed_share() > 0.0

    escalated_root = next(r.run_id for r in propagated.runs if r.escalated)
    assert propagated.cost.per_run_nanocents(
        escalated_root
    ) > broken.cost.per_run_nanocents(escalated_root)


def test_recording_one_span_twice_overwrites_rather_than_adds() -> None:
    """Idempotent under replay, which Chapter 24's runner requires."""
    ledger = CostLedger()
    ledger.record(
        model="fake-model-1",
        input_tokens=4120,
        cached_input_tokens=3800,
        output_tokens=210,
        run_id="run-1",
        span_id="model:3",
    )
    once = ledger.per_run_nanocents("run-1")
    ledger.record(
        model="fake-model-1",
        input_tokens=4120,
        cached_input_tokens=3800,
        output_tokens=210,
        run_id="run-1",
        span_id="model:3",
    )
    assert ledger.per_run_nanocents("run-1") == once
    assert len(ledger.events) == 1


def test_the_cached_split_is_computed_not_declared() -> None:
    """The cached part of this turn's prompt is what the last turn sent."""
    assert cached_split(4120, 3800) == (320, 3800)
    assert cached_split(4120, 0) == (4120, 0)
    assert cached_split(100, 4000) == (0, 100)


def test_ignoring_the_cached_split_overstates_the_bill() -> None:
    """Wrong in the direction that gets a project cancelled."""
    ledger = CostLedger()
    ledger.record(
        model="fake-model-1",
        input_tokens=100_000,
        cached_input_tokens=90_000,
        output_tokens=1_000,
        run_id="split",
        span_id="s1",
    )
    naive = CostLedger()
    naive.record(
        model="fake-model-1",
        input_tokens=100_000,
        cached_input_tokens=0,
        output_tokens=1_000,
        run_id="naive",
        span_id="s1",
    )
    assert naive.per_run_nanocents("naive") > ledger.per_run_nanocents("split")


def test_a_cached_portion_larger_than_the_prompt_is_refused() -> None:
    """It would mean the split was computed against the wrong turn."""
    with pytest.raises(ValueError, match="cannot exceed"):
        CostLedger().record(
            model="fake-model-1",
            input_tokens=10,
            cached_input_tokens=11,
        )


def test_every_event_carries_a_pricing_version() -> None:
    """So a rate-card change cannot silently rewrite last month."""
    ledger = CostLedger()
    ledger.record(
        model="fake-model-1", input_tokens=10, run_id="r", span_id="s"
    )
    assert ledger.events[0].pricing_version == PRICING_VERSION


def test_an_unknown_model_can_be_made_to_raise() -> None:
    """Guessing a rate is how a cost dashboard becomes fiction."""
    strict = CostLedger(prices={}, strict=True)
    with pytest.raises(KeyError, match="no price for model"):
        strict.price_for("some-new-model")


def test_cost_per_success_counts_the_runs_that_produced_nothing(
    propagated,  # noqa: ANN001
) -> None:
    """The right denominator, with human minutes in the numerator."""
    assert len(propagated.successes) == 3
    assert len(propagated.runs) == 4
    per_run = propagated.cost_per_run()
    per_success = propagated.cost_per_success()
    assert per_success > per_run

    without_humans = propagated.cost.cost_per_success(
        attempted=[r.run_id for r in propagated.runs],
        succeeded=propagated.successes,
    )
    assert per_success > without_humans


def test_nothing_succeeded_reports_infinity_not_an_error() -> None:
    """The honest answer rather than a division error."""
    ledger = CostLedger()
    ledger.record(model="fake-model-1", input_tokens=10, run_id="r")
    assert ledger.cost_per_success(attempted=["r"], succeeded=[]) == float(
        "inf"
    )


def test_the_escalated_run_reported_success_and_resolved_nothing(
    propagated,  # noqa: ANN001
) -> None:
    """Graded on its own status field it is a clean run."""
    escalated = next(r for r in propagated.runs if r.escalated)
    assert escalated.status == "succeeded"
    assert escalated.verified is False
    assert escalated.world.total_refunded_cents(escalated.ticket.order_id) == 0
    assert any(
        case["status"] == "open" for case in escalated.world.escalations
    )


# ------------------------------------------------------------- the payload


def test_the_three_buckets_are_exclusive() -> None:
    """A policy that says two things about one field is unreviewable."""
    with pytest.raises(ValueError, match="more than one bucket"):
        Redactor(drop=["arguments.order_id"], hash=["arguments.order_id"])


def test_the_message_body_never_reaches_an_exporter(
    propagated,  # noqa: ANN001
) -> None:
    """Dropped in-process. Dropping in the collector is a disclosure."""
    messages = [
        s
        for s in spans_named(propagated, SPAN_NAMES["tool"])
        if s.attributes["gen_ai.tool.name"].startswith("send_message")
    ]
    assert messages
    for span in messages:
        exported = span.attributes["northstar.tool.arguments"]
        assert "body" not in exported
        assert str(exported["order_id"]).startswith("sha256:")


def test_business_facts_survive_because_reconciliation_needs_them() -> None:
    """Amounts in cents are evidence, not personal data."""
    kept = REDACTOR.apply(
        {
            "arguments": {
                "order_id": "NR-2026-0041827",
                "amount_cents": 3250,
                "reason": "damaged",
                "body": "please refund me, my email is ada@example.com",
            }
        }
    )["arguments"]
    assert kept["amount_cents"] == 3250
    assert kept["reason"] == "damaged"
    assert "body" not in kept
    assert kept["order_id"] != "NR-2026-0041827"


def test_an_unclassified_field_still_gets_scrubbed() -> None:
    """The safe direction: no bucket does not mean no check."""
    out = REDACTOR.apply({"arguments": {"note": "reply to ada@example.com"}})
    assert "ada@example.com" not in out["arguments"]["note"]
    assert REDACTOR.classify("arguments.note") == "default"


def test_the_policy_extends_the_package_rather_than_forking_it() -> None:
    """One redactor in the repository, not two that drift."""
    assert isinstance(REDACTOR, BaseRedactor)


# ------------------------------------------------- the mapping layer itself


def test_a_convention_rename_is_one_edit() -> None:
    """Internal field names in, current convention names out."""
    world = World()
    ctx = build_context(
        "run-map",
        "refund the lamp shade",
        principal=SUPPORT_PRINCIPAL,
        specs=world.tool_specs(),
    )
    from northstar_contracts import ToolCall

    call = ToolCall("c1", "issue_refund", {"amount_cents": 3250})
    attributes = tool_span_attributes(call, ctx)

    assert attributes[CONVENTION["operation"]] == "execute_tool"
    assert attributes[CONVENTION["tool_call_id"]] == "c1"
    # The arguments themselves are never on the span; only a digest is.
    assert "3250" not in str(attributes[CONVENTION["tool_name"]])
    assert len(attributes["northstar.tool.args_digest"]) == 16
    assert attributes["northstar.config.hash"] == CONFIG_HASH


def test_the_digest_groups_identical_calls_without_reading_them() -> None:
    """Which is what lets you detect the repeated-step failure mode."""
    world = World()
    ctx = build_context(
        "run-digest",
        "goal",
        principal=SUPPORT_PRINCIPAL,
        specs=world.tool_specs(),
    )
    from northstar_contracts import ToolCall

    same_a = tool_span_attributes(
        ToolCall("c1", "get_order", {"order_id": "NR-2026-0041827"}), ctx
    )
    same_b = tool_span_attributes(
        ToolCall("c2", "get_order", {"order_id": "NR-2026-0041827"}), ctx
    )
    other = tool_span_attributes(
        ToolCall("c3", "get_order", {"order_id": "NR-2026-0041903"}), ctx
    )
    assert (
        same_a["northstar.tool.args_digest"]
        == same_b["northstar.tool.args_digest"]
    )
    assert (
        same_a["northstar.tool.args_digest"]
        != other["northstar.tool.args_digest"]
    )


def test_the_side_effect_index_reads_the_receipt_not_the_status() -> None:
    """A tool reporting success and a ledger recording a commit differ."""
    from northstar_contracts import ToolCall, ToolResult

    index = SideEffectIndex()
    call = ToolCall("c9", "issue_refund", {"order_id": "NR-2026-0041827"})
    index.record(
        "run-x",
        call,
        ToolResult(
            call_id="c9",
            ok=True,
            content={"refund_id": "RFND-00007", "duplicate": True},
        ),
        {"idempotency_key": "abc123"},
    )
    captured = index.for_call("run-x", "c9")
    assert captured["side_effect_id"] == "RFND-00007"
    assert captured["idempotency_key"] == "abc123"
    assert captured["duplicate"] is True
    assert index.for_call("run-x", "nope")["side_effect_id"] == ""


def test_a_price_prices_cached_input_on_its_own_terms() -> None:
    """Because an agent loop is the workload most affected by caching."""
    from northstar_telemetry import ModelPrice

    price = Price(
        version="test",
        uncached=ModelPrice(300, 1500),
        cached_input_cents_per_million=30,
    )
    uncached_only = price.nanocents_for(1_000_000, 0, 0)
    cached_only = price.nanocents_for(0, 1_000_000, 0)
    assert uncached_only == 10 * cached_only
