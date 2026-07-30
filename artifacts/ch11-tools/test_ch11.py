"""The Chapter 11 properties, as assertions.

The demo prints; this fails a build. Every assertion is about behaviour -- what
the world holds after a refund, how many tokens a result actually costs, what
registration refuses, which trajectory the gate rejects -- rather than about
the text of a message.

The strings that *are* asserted are the ones where the string is the contract:
``issue_refund``'s three output field names, its version, the stable error code
``amount_exceeds_order_total``, and the three sections a description must
carry. Each of those is something another component reads at runtime, so a
silent change to any of them is a silent change to behaviour.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import inspect
from typing import Any

import demo
import pytest
from budget import CURSOR_TOKENS, count_tokens, enforce_budget, fit, shape
from conformance import (
    DESCRIPTION_BUDGET,
    NAME_RE,
    REQUIRED_SECTIONS,
    ConformanceError,
    ConformingRegistry,
    check,
    check_library,
    required,
)
from golden import (
    DAMAGED_TICKET,
    GOLDEN_TRAJECTORY,
    LAMP_SHADE_CENTS,
    ORDER_TOTAL_CENTS,
    ReadsTheDescription,
    description_is_complete,
    outcome_gate,
    path_of,
    trajectory_gate,
)
from library import Library, build_library, unshaped_search_orders_of
from lint_results import FIXTURES, ResultProbe, bloated_world, lint
from northstar_contracts import (
    ToolCall,
    ToolSpec,
    ToolTimeout,
    ToolValidationError,
    World,
    idempotency_key,
)
from northstar_runtime import AgentLoop
from refund import (
    POLICY_REASON,
    RefundPath,
    SideEffectLedger,
    cancel_refund,
    issue_refund,
    preview_refund,
)
from sandbox import NullSandbox, SandboxContract, SandboxDenied
from specs import (
    APPROVAL_REQUIRED,
    BROAD_CAPABILITIES,
    COMPENSATIONS,
    ISSUE_REFUND,
    MAX_REFUND_CENTS,
    PREVIEW_REFUND,
    REFUND_REASONS,
    SEARCH_ORDERS,
    SEARCH_ORDERS_DRIFTED,
    SPECS,
    compensation_for,
    spec_for,
)

ORDER = "NR-2026-0041827"
FRAUD_ORDER = "NR-2026-0042110"
APPROVAL_THRESHOLD_CENTS = 5000
RUN_ID = "run-ch11-tools"


def a_spec(**overrides: Any) -> ToolSpec:
    """A minimal conformant contract, for the tests that break one rule."""
    base: dict[str, Any] = {
        "name": "close_case",
        "description": (
            "Close a resolved case. Use this when the customer has "
            "confirmed. Returns the case id. Does not refund anything."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"case_id": {"type": "string"}},
            "required": ["case_id"],
            "additionalProperties": False,
        },
        "output_schema": {
            "type": "object",
            "properties": {"case_id": {"type": "string"}},
            "additionalProperties": False,
        },
        "writes": False,
        "idempotent": True,
    }
    base.update(overrides)
    return ToolSpec(**base)


def a_fn(case_id: str) -> dict[str, Any]:
    """An implementation matching :func:`a_spec`."""
    return {"case_id": case_id}


# --------------------------------------------------- the version-critical facts
#
# An external audit caught defects here. Each of the five assertions below is
# one of them, and each is something another component reads at runtime.


def test_issue_refund_declares_the_contract_the_chapter_prints() -> None:
    """200-token budget, version 3, and a receipt-shaped output."""
    assert ISSUE_REFUND.max_result_tokens == 200
    assert ISSUE_REFUND.version == "3"
    assert sorted(ISSUE_REFUND.output_schema["properties"]) == [
        "amount_cents",
        "receipt_id",
        "status",
    ]
    assert ISSUE_REFUND.writes is True
    assert ISSUE_REFUND.idempotent is True
    assert "idempotency_key" in required(ISSUE_REFUND.input_schema)


def test_the_dry_run_is_a_separate_read_only_tool() -> None:
    """Not a flag. A flag leaves the call registered and authorized as a write."""
    assert PREVIEW_REFUND.writes is False
    assert "dry_run" not in ISSUE_REFUND.input_schema["properties"]
    assert "dry_run" not in PREVIEW_REFUND.input_schema["properties"]
    assert set(PREVIEW_REFUND.input_schema["properties"]) <= set(
        ISSUE_REFUND.input_schema["properties"]
    )


def test_the_library_prefers_three_narrow_tools(library: Library) -> None:
    """``get_order``, ``preview_refund``, ``issue_refund`` is the shape."""
    names = library.registry.names()
    for name in ("get_order", "preview_refund", "issue_refund"):
        assert name in names
    assert not BROAD_CAPABILITIES & set(names)
    assert library.read_only_names() == [
        "get_order",
        "get_policy",
        "search_orders",
        "preview_refund",
    ]


def test_the_refund_output_actually_carries_those_three_fields(
    path: RefundPath,
) -> None:
    """The declaration and the implementation agree, or the shape is fiction."""
    receipt = path.issue_refund(
        ORDER, LAMP_SHADE_CENTS, "damaged", "k" * 32
    )
    assert sorted(receipt) == ["amount_cents", "receipt_id", "status"]
    assert receipt["amount_cents"] == LAMP_SHADE_CENTS
    assert receipt["status"] == "settled"
    assert count_tokens(receipt) <= ISSUE_REFUND.max_result_tokens


def test_a_broad_capability_cannot_be_registered() -> None:
    """``run_sql``'s permission set is whatever its interface can reach."""
    for name in ("run_sql", "call_api", "execute_shell"):
        spec = a_spec(name=name)
        problems = check(spec, a_fn)
        assert any("broad capability" in p for p in problems), name
        with pytest.raises(ConformanceError):
            ConformingRegistry().register(spec, a_fn)


# ------------------------------------------------------- the conformance suite


def test_the_whole_library_is_conformant(library: Library) -> None:
    """Nothing registered fails a rule, and nothing overlaps."""
    for spec, fn in library.registry.bindings():
        assert check(spec, fn) == [], spec.name
    assert library.registry.check_library() == []


def test_registration_is_the_gate_not_a_test(library: Library) -> None:
    """A tool that fails cannot be registered, which a test cannot promise."""
    registry = ConformingRegistry()
    with pytest.raises(ConformanceError) as caught:
        registry.register(a_spec(name="Bad Name"), a_fn)
    assert "snake_case" in str(caught.value)
    assert len(registry) == 0
    assert "Bad Name" not in registry


def test_an_idempotent_write_must_require_a_key() -> None:
    """The most dangerous lie a contract can tell."""
    spec = a_spec(name="issue_credit", writes=True, idempotent=True)
    problems = check(spec, a_fn)
    assert "idempotent write must require a key" in problems

    keyed = a_spec(
        name="issue_credit",
        writes=True,
        idempotent=True,
        input_schema={
            "type": "object",
            "properties": {
                "case_id": {"type": "string"},
                "idempotency_key": {"type": "string"},
            },
            "required": ["case_id", "idempotency_key"],
            "additionalProperties": False,
        },
    )

    def credit(case_id: str, idempotency_key: str) -> dict[str, Any]:
        return {"case_id": case_id}

    assert "idempotent write must require a key" not in check(keyed, credit)


def test_every_write_is_reversible_or_needs_approval() -> None:
    """The set of irreversible unattended actions is empty by construction."""
    writes = [s.name for s in SPECS.values() if s.writes]
    assert writes
    for name in writes:
        assert compensation_for(name) or name in APPROVAL_REQUIRED, name
    orphan = a_spec(name="delete_order", writes=True, idempotent=False)
    assert (
        "write with no compensation and no approval rule"
        in check(orphan, a_fn)
    )


def test_a_description_must_answer_the_three_questions() -> None:
    """"Does not" is the sentence teams skip and the model needs most."""
    for section in REQUIRED_SECTIONS:
        spec = a_spec(
            description=a_spec().description.replace(section, "Blah")
        )
        assert f"description missing section: {section}" in check(spec, a_fn)
    for spec in SPECS.values():
        for section in REQUIRED_SECTIONS:
            assert section in spec.description, (spec.name, section)


def test_descriptions_stay_inside_the_token_budget() -> None:
    """Descriptions are billed on every turn of every run."""
    for spec in SPECS.values():
        assert len(spec.description) <= DESCRIPTION_BUDGET, spec.name
    fat = a_spec(description=a_spec().description + "x" * DESCRIPTION_BUDGET)
    assert any("over budget" in p for p in check(fat, a_fn))


def test_a_strict_input_schema_is_required() -> None:
    """Without it, an invented argument is a silent success."""
    loose = a_spec(
        input_schema={
            "type": "object",
            "properties": {"case_id": {"type": "string"}},
            "required": ["case_id"],
        }
    )
    assert "input_schema must forbid extra properties" in check(loose, a_fn)
    for spec in SPECS.values():
        assert spec.input_schema["additionalProperties"] is False, spec.name


def test_an_output_schema_is_required() -> None:
    """It is what lets a result be truncated, redacted, and graded."""
    assert "output_schema is required" in check(
        a_spec(output_schema={}), a_fn
    )
    assert any(
        "declares no properties" in p
        for p in check(
            a_spec(output_schema={"type": "object", "properties": {}}), a_fn
        )
    )
    for spec in SPECS.values():
        assert spec.output_schema.get("properties"), spec.name


def test_the_implementation_must_be_callable_from_the_schema() -> None:
    """The registry calls ``fn(**arguments)``; a mismatch is a TypeError."""

    def wrong(case_id: str, tenant: str) -> dict[str, Any]:
        return {}

    problems = check(a_spec(), wrong)
    assert any("does not declare: tenant" in p for p in problems)

    def narrow(case_id: str) -> dict[str, Any]:
        return {}

    two_fields = a_spec(
        input_schema={
            "type": "object",
            "properties": {
                "case_id": {"type": "string"},
                "note": {"type": "string"},
            },
            "required": ["case_id"],
            "additionalProperties": False,
        }
    )
    assert any(
        "cannot accept declared argument(s): note" in p
        for p in check(two_fields, narrow)
    )


def test_the_budget_wrapper_does_not_hide_the_signature(
    library: Library,
) -> None:
    """``functools.wraps`` is load-bearing: without it the check is disabled."""
    registered = {
        spec.name: fn for spec, fn in library.registry.bindings()
    }
    signature = inspect.signature(registered["issue_refund"])
    assert set(signature.parameters) == {
        "order_id",
        "amount_cents",
        "reason",
        "idempotency_key",
    }


def test_the_overlap_test_fires_on_two_interchangeable_tools() -> None:
    """An ambiguity in the tool list is not fixable from the system prompt."""
    twin = ToolSpec(
        name="find_orders",
        description=SEARCH_ORDERS.description,
        input_schema=SEARCH_ORDERS.input_schema,
        output_schema=SEARCH_ORDERS.output_schema,
        writes=False,
        idempotent=True,
    )
    problems = check_library([SEARCH_ORDERS, twin])
    assert any("pick either" in p for p in problems)
    assert check_library(list(SPECS.values())) == []


def test_a_dry_run_flag_is_refused() -> None:
    """The rule that only exists between tools."""
    flagged = a_spec(
        name="issue_credit",
        writes=True,
        idempotent=False,
        input_schema={
            "type": "object",
            "properties": {
                "case_id": {"type": "string"},
                "dry_run": {"type": "boolean"},
            },
            "required": ["case_id"],
            "additionalProperties": False,
        },
    )
    problems = check_library([flagged])
    assert any("dry run is a separate" in p for p in problems)


def test_the_name_pattern_wants_a_verb_and_a_noun() -> None:
    """The model matches intent against names before it reads a word."""
    for name in ("get_order", "issue_refund", "preview_refund", "run_code"):
        assert NAME_RE.match(name), name
    for name in ("Order", "getOrder", "order", "get order", "_get_order"):
        assert not NAME_RE.match(name), name


# ------------------------------------------------------- budgets and shaping


def test_shaping_drops_undeclared_fields(world: World) -> None:
    """The budget is spent on fields somebody chose."""
    raw = world.get_order(ORDER)
    assert "customer_id" in raw
    shaped = shape(raw, spec_for("get_order").output_schema)
    assert "customer_id" not in shaped
    assert "placed_at" not in shaped
    assert set(shaped) <= set(
        spec_for("get_order").output_schema["properties"]
    )
    assert count_tokens(shaped) < count_tokens(raw)


def test_shaping_reaches_inside_arrays(world: World) -> None:
    """Rows get trimmed too, or the row count defeats the whole exercise."""
    raw = world.get_order(ORDER)
    shaped = shape(raw, spec_for("get_order").output_schema)
    assert shaped["items"]
    for item in shaped["items"]:
        assert sorted(item) == [
            "name",
            "quantity",
            "sku",
            "unit_price_cents",
        ]


def test_the_unshaped_search_is_a_multiple_of_its_cap() -> None:
    """A result size that is a function of the data, not of the contract."""
    world = bloated_world()
    raw = unshaped_search_orders_of(world)(customer_id="CUST-8841")
    cap = SEARCH_ORDERS.max_result_tokens
    assert count_tokens(raw) > 6000
    assert count_tokens(raw) > cap * 6
    shaped = shape(raw, SEARCH_ORDERS.output_schema)
    assert count_tokens(shaped) < count_tokens(raw) / 3


def test_enforce_budget_shapes_then_truncates_then_says_so() -> None:
    """The three properties, in one assertion each."""
    world = bloated_world()
    raw = unshaped_search_orders_of(world)(customer_id="CUST-8841")
    result = enforce_budget(SEARCH_ORDERS, {**raw, "call_id": "c1"})
    assert count_tokens(result.content) <= SEARCH_ORDERS.max_result_tokens
    assert result.truncated is True
    assert result.content["cursor"]
    assert "cursor=" in result.content["note"]
    assert 0 < len(result.content["results"]) < raw["total_matches"]
    assert result.call_id == "c1"
    assert "call_id" not in result.content


def test_a_result_inside_its_budget_is_not_flagged(world: World) -> None:
    """Truncation is not a default; it is a thing that happened."""
    raw = world.get_order(ORDER)
    result = enforce_budget(
        spec_for("get_order"), {**raw, "call_id": "c1"}
    )
    assert result.truncated is False
    assert "note" not in result.content
    assert result.content["order_id"] == ORDER


def test_fit_leaves_room_for_the_cursor() -> None:
    """Budgeting to the exact cap and then adding a note blows the cap."""
    payload = {"results": [{"order_id": f"o{i}"} for i in range(200)]}
    head = fit(payload, 100 - CURSOR_TOKENS)
    assert count_tokens(head) <= 100
    assert head["omitted_items"] > 0


def test_fit_bounds_a_result_with_no_list_at_all() -> None:
    """Lossy, and bounded beats faithful when the alternative is unbounded."""
    payload = {"blob": "x" * 40000}
    head = fit(payload, 50)
    assert count_tokens(head) <= 120
    assert "preview" in head


def test_the_declared_caps_are_all_positive() -> None:
    """A cap of zero is not a budget, it is a broken tool."""
    for spec in SPECS.values():
        assert spec.max_result_tokens > 0, spec.name
    assert "max_result_tokens must be positive" in check(
        a_spec(max_result_tokens=0), a_fn
    )


# ------------------------------------------------------------------ the lint


def test_the_lint_passes_over_the_wired_library() -> None:
    """Every fixture result fits its declared budget."""
    library = build_library(bloated_world())
    probe = ResultProbe(library.registry, enforce=True)
    lines: list[str] = []
    assert lint(FIXTURES, registry=probe, out=lines.append) == 0
    assert lines == []


def test_the_lint_fails_over_the_unwired_one() -> None:
    """Oversized *and* unflagged: the chapter's two failures, both reported."""
    library = build_library(bloated_world(), unshaped_search=True)
    probe = ResultProbe(library.registry, enforce=False)
    lines: list[str] = []
    failures = lint(FIXTURES, registry=probe, out=lines.append)
    assert failures == 2
    assert any("tokens > cap 800" in line for line in lines)
    assert any("over cap and not flagged" in line for line in lines)
    assert any("6" in line for line in lines)


def test_the_lint_measures_the_tool_not_the_runtime_safety_net() -> None:
    """A probe over the runtime's truncation would call everything fine."""
    library = build_library(bloated_world(), unshaped_search=True)
    through_runtime = library.registry.dispatch(
        ToolCall("c1", "search_orders", {"customer_id": "CUST-8841"}),
        run_id=RUN_ID,
        step=1,
    )
    assert count_tokens(through_runtime.content) <= (
        SEARCH_ORDERS.max_result_tokens
    )
    assert through_runtime.truncated is True

    probe = ResultProbe(library.registry, enforce=False)
    raw = probe.dispatch(ToolCall("c1", "search_orders", {}))
    assert count_tokens(raw.content) > SEARCH_ORDERS.max_result_tokens


# ---------------------------------------------------- preview, commit, ledger


def test_a_preview_moves_nothing(world: World, ledger: SideEffectLedger) -> None:
    """``writes=False`` is a claim; this is the check on it."""
    before = world.snapshot()
    result = preview_refund(ORDER, LAMP_SHADE_CENTS, "damaged", world=world)
    assert result["amount_cents"] == LAMP_SHADE_CENTS
    assert result["requires_approval"] is False
    assert world.snapshot() == before
    assert world.refunds == []
    assert world.ledger == []
    assert ledger.rows == []


def test_a_preview_above_the_threshold_says_so(world: World) -> None:
    """The approval decision is made from the preview, not from the commit."""
    result = preview_refund(ORDER, ORDER_TOTAL_CENTS, "damaged", world=world)
    assert ORDER_TOTAL_CENTS > APPROVAL_THRESHOLD_CENTS
    assert result["requires_approval"] is True
    assert result["resulting_status"] == "refunded"
    assert world.refunds == []


def test_the_error_carries_a_code_and_a_next_action(world: World) -> None:
    """An error result is read by a model deciding what to do next."""
    with pytest.raises(ToolValidationError) as caught:
        preview_refund(ORDER, ORDER_TOTAL_CENTS + 1, "damaged", world=world)
    message = str(caught.value)
    assert "amount_exceeds_order_total" in message
    assert str(ORDER_TOTAL_CENTS) in message
    assert "Refund at most" in message
    assert "escalate_to_specialist" in message


def test_the_commit_is_idempotent_on_its_key(
    world: World,
    ledger: SideEffectLedger,
) -> None:
    """Same key, same receipt, one refund row."""
    key = idempotency_key(run_id=RUN_ID, step_id=3)
    first = issue_refund(
        ORDER, LAMP_SHADE_CENTS, "damaged", key,
        world=world, ledger=ledger,
    )
    again = issue_refund(
        ORDER, LAMP_SHADE_CENTS, "damaged", key,
        world=world, ledger=ledger,
    )
    assert first["receipt_id"] == again["receipt_id"]
    assert first["status"] == "settled"
    assert again["status"] == "duplicate"
    assert len(world.refunds) == 1
    assert world.total_refunded_cents(ORDER) == LAMP_SHADE_CENTS
    assert len(ledger.rows) == 1


def test_the_ledger_records_intent_before_the_call(
    world: World,
    ledger: SideEffectLedger,
) -> None:
    """A ledger that only records successes cannot support reconciliation."""
    world.inject_fault("issue_refund", kind="timeout")
    key = idempotency_key(run_id=RUN_ID, step_id=7)
    with pytest.raises(ToolTimeout):
        issue_refund(
            ORDER, LAMP_SHADE_CENTS, "damaged", key,
            world=world, ledger=ledger,
        )
    assert len(ledger.rows) == 1
    assert ledger.rows[0].outcome == "unknown"
    assert ledger.unresolved() == ledger.rows
    assert len(world.refunds) == 1        # the write did land
    assert ledger.receipts() == []        # and the caller cannot know


def test_reconciling_a_timeout_with_the_same_key_pays_once(
    world: World,
    ledger: SideEffectLedger,
) -> None:
    """The recovery the unknown row makes possible."""
    world.inject_fault("issue_refund", kind="timeout")
    key = idempotency_key(run_id=RUN_ID, step_id=7)
    with pytest.raises(ToolTimeout):
        issue_refund(
            ORDER, LAMP_SHADE_CENTS, "damaged", key,
            world=world, ledger=ledger,
        )
    settled = issue_refund(
        ORDER, LAMP_SHADE_CENTS, "damaged", key,
        world=world, ledger=ledger,
    )
    assert settled["status"] == "duplicate"
    assert len(world.refunds) == 1
    assert ledger.unresolved() == []
    assert len(ledger.receipts()) == 1


def test_recording_an_outcome_with_no_intent_raises(
    ledger: SideEffectLedger,
) -> None:
    """The order of the two writes is the whole point of the ledger."""
    with pytest.raises(KeyError, match="before the external call"):
        ledger.record_outcome(key="never-recorded", receipt={"status": "ok"})


def test_the_ledger_row_names_the_version_and_the_inverse(
    path: RefundPath,
) -> None:
    """A resumed run and an auditor both read the row, not today's code."""
    path.issue_refund(ORDER, LAMP_SHADE_CENTS, "damaged", "k" * 32)
    row = path.ledger.rows[0]
    assert row.version == "3"
    assert row.compensation == "cancel_refund"
    assert row.principal == "northstar-support-agent"
    assert len(row.args_fingerprint) == 16
    assert path.ledger.irreversible() == []


def test_receipts_and_refunds_agree(path: RefundPath) -> None:
    """The invariant the nightly reconciliation job asserts."""
    path.issue_refund(ORDER, LAMP_SHADE_CENTS, "damaged", "k" * 32)
    path.issue_refund(FRAUD_ORDER, 1200, "not_received", "j" * 32)
    assert len(path.ledger.receipts()) == len(path.world.refunds) == 2


def test_the_compensation_is_recorded_not_merely_documented(
    path: RefundPath,
) -> None:
    """A supervisor can undo a step without inventing a procedure."""
    receipt = path.issue_refund(ORDER, LAMP_SHADE_CENTS, "damaged", "k" * 32)
    reversal = cancel_refund(receipt["receipt_id"], ledger=path.ledger)
    assert reversal["compensated_by"] == "cancel_refund"
    assert "72 hours" in reversal["window"]
    assert path.ledger.rows[0].outcome == "compensated"
    with pytest.raises(KeyError):
        cancel_refund("RFND-99999", ledger=path.ledger)


def test_a_message_declares_that_it_does_not_restore() -> None:
    """A duplicate apology has already been read. Price it as trust."""
    message = COMPENSATIONS["send_message"]
    assert message.restores is False
    assert message.cost == "customer trust"
    assert COMPENSATIONS["issue_refund"].restores is True


def test_the_two_reason_vocabularies_are_mapped_not_conflated() -> None:
    """The tool's enum is the caller's; the store's is the store's."""
    assert set(POLICY_REASON) == set(REFUND_REASONS)
    assert POLICY_REASON["not_received"] == "not_delivered"
    assert POLICY_REASON["wrong_item"] == "damaged"


def test_the_amount_bound_fails_closed(library: Library) -> None:
    """A schema that rejects a 900,000-cent refund is a control."""
    assert (
        ISSUE_REFUND.input_schema["properties"]["amount_cents"]["maximum"]
        == MAX_REFUND_CENTS
    )
    over = library.registry.dispatch(
        ToolCall(
            "c1",
            "issue_refund",
            {
                "order_id": ORDER,
                "amount_cents": 900000,
                "reason": "damaged",
            },
        ),
        run_id=RUN_ID,
        step=1,
    )
    assert over.ok is False
    assert library.world.refunds == []


def test_a_reason_outside_the_enum_is_refused(library: Library) -> None:
    """A ``reason`` typed as a string invites an invented taxonomy."""
    result = library.registry.dispatch(
        ToolCall(
            "c1",
            "issue_refund",
            {
                "order_id": ORDER,
                "amount_cents": 100,
                "reason": "customer_seemed_upset",
            },
        ),
        run_id=RUN_ID,
        step=1,
    )
    assert result.ok is False or library.world.refunds == []


# -------------------------------------------------------- the registry's stamp


def test_the_registry_stamps_the_key_before_validating(
    library: Library,
) -> None:
    """The key is required, and the model is not the one who supplies it."""
    call = ToolCall(
        "c1",
        "issue_refund",
        {
            "order_id": ORDER,
            "amount_cents": LAMP_SHADE_CENTS,
            "reason": "damaged",
        },
    )
    result = library.registry.dispatch(call, run_id=RUN_ID, step=3)
    assert result.ok, result.content
    assert library.ledger.rows[0].key == idempotency_key(
        run_id=RUN_ID, step_id="3:c1"
    )


def test_a_replayed_step_pays_once(library: Library) -> None:
    """The same run and step derive the same key, so the retry is a no-op."""
    call = ToolCall(
        "c1",
        "issue_refund",
        {
            "order_id": ORDER,
            "amount_cents": LAMP_SHADE_CENTS,
            "reason": "damaged",
        },
    )
    library.registry.dispatch(call, run_id=RUN_ID, step=3)
    library.registry.dispatch(call, run_id=RUN_ID, step=3)
    assert len(library.world.refunds) == 1
    assert library.registry.is_retry_safe(call) is True


def test_a_call_with_no_run_identity_is_refused(library: Library) -> None:
    """Nothing to derive from means the key is missing, which is correct."""
    result = library.registry.dispatch(
        ToolCall(
            "c1",
            "issue_refund",
            {
                "order_id": ORDER,
                "amount_cents": LAMP_SHADE_CENTS,
                "reason": "damaged",
            },
        )
    )
    assert result.ok is False
    assert "idempotency_key" in str(result.content)
    assert library.world.refunds == []


# ------------------------------------------------------------- code execution


def test_the_sandbox_denies_egress_structurally(
    sandbox: NullSandbox,
) -> None:
    """Not a policy the program is asked to respect; a namespace it lacks."""
    for program in (
        "import socket",
        "import urllib.request",
        "__import__('os')",
    ):
        with pytest.raises(SandboxDenied):
            sandbox.run(program)
    assert sandbox.contract.egress == "deny"


def test_the_sandbox_cannot_reach_the_filesystem_or_other_tools(
    sandbox: NullSandbox,
) -> None:
    """Its real authority is what the namespace holds, and that is the point."""
    for program in (
        "open('/etc/passwd')",
        "print(issue_refund('NR-2026-0041827', 8400, 'damaged'))",
        "print(globals())",
        "eval('1+1')",
    ):
        with pytest.raises(SandboxDenied):
            sandbox.run(program)


def test_the_sandbox_runs_the_aggregation_it_is_for(
    sandbox: NullSandbox,
) -> None:
    """The saving: rows aggregated without entering the context window."""
    result = sandbox.run(
        "print(sum(inputs['cents']))", {"cents": [5150, 3250]}
    )
    assert result["stdout"].strip() == "8400"
    assert result["truncated"] is False
    assert result["wall_seconds"] >= 0


def test_the_sandbox_budgets_its_output(sandbox: NullSandbox) -> None:
    """The result budget is whatever the program prints, unless something caps it."""
    result = sandbox.run("print('x' * 40000)")
    assert result["truncated"] is True
    assert count_tokens(result["stdout"]) <= (
        sandbox.contract.max_stdout_tokens + 20
    )


def test_a_contract_with_a_credential_will_not_start() -> None:
    """A database credential in the sandbox is a write tool in disguise."""
    for environment in (
        {"DATABASE_URL": "postgres://user:pw@host/db"},
        {"STRIPE_API_KEY": "sk_live_x"},
        {"AWS_SESSION_TOKEN": "x"},
    ):
        with pytest.raises(SandboxDenied, match="credential"):
            NullSandbox(SandboxContract(environment=environment))


def test_a_contract_that_relaxes_a_term_will_not_start() -> None:
    """The terms that get quietly relaxed are the ones nobody re-reads."""
    for contract in (
        SandboxContract(egress="allow"),
        SandboxContract(user="root"),
        SandboxContract(filesystem="persistent"),
        SandboxContract(image="python:latest"),
        SandboxContract(max_wall_seconds=0),
    ):
        assert contract.problems()
        with pytest.raises(SandboxDenied):
            NullSandbox(contract)


def test_run_code_declares_itself_honestly() -> None:
    """A write, not idempotent, and behind approval because it has no inverse."""
    spec = spec_for("run_code")
    assert spec.writes is True
    assert spec.idempotent is False
    assert compensation_for("run_code") is None
    assert "run_code" in APPROVAL_REQUIRED


def test_a_sandbox_error_carries_no_internal_detail(
    sandbox: NullSandbox,
) -> None:
    """Every byte of an error goes into the model's context."""
    with pytest.raises(SandboxDenied) as caught:
        sandbox.run("1 / 0")
    message = str(caught.value)
    assert message.startswith("ZeroDivisionError")
    assert "Traceback" not in message
    assert "/Users/" not in message
    assert "site-packages" not in message


# ------------------------------------------------------- the trajectory gate


def test_the_golden_trajectory_passes_its_own_gates(world: World) -> None:
    """A gate the correct run fails is a gate nobody will keep."""
    library = build_library(world)
    run = AgentLoop(
        model=ReadsTheDescription(), tools=library.registry, max_turns=8
    ).run(DAMAGED_TICKET, run_id=RUN_ID)
    assert path_of(run) == [
        "search_orders",
        "get_order",
        "get_policy",
        "preview_refund",
        "issue_refund",
    ]
    for name in GOLDEN_TRAJECTORY:
        assert name in path_of(run)
    assert trajectory_gate().grade(run, world).passed
    assert outcome_gate().grade(run, world).passed
    assert world.total_refunded_cents(ORDER) == LAMP_SHADE_CENTS


def test_the_description_diff_fails_the_trajectory_gate(
    world: World,
) -> None:
    """The four-line diff, and the only gate that catches it."""
    library = build_library(
        world, search_description=SEARCH_ORDERS_DRIFTED
    )
    run = AgentLoop(
        model=ReadsTheDescription(), tools=library.registry, max_turns=8
    ).run(DAMAGED_TICKET, run_id=RUN_ID)
    assert "get_order" not in path_of(run)
    verdict = trajectory_gate().grade(run, world)
    assert verdict.passed is False
    assert any("get_order" in reason for reason in verdict.reasons)


def test_the_description_diff_overpays_the_customer(world: World) -> None:
    """The outcome gate catches the amount; the trajectory gate the path."""
    library = build_library(
        world, search_description=SEARCH_ORDERS_DRIFTED
    )
    run = AgentLoop(
        model=ReadsTheDescription(), tools=library.registry, max_turns=8
    ).run(DAMAGED_TICKET, run_id=RUN_ID)
    assert world.total_refunded_cents(ORDER) == ORDER_TOTAL_CENTS
    assert world.total_refunded_cents(ORDER) - LAMP_SHADE_CENTS == 5150
    verdict = outcome_gate().grade(run, world)
    assert verdict.passed is False
    assert any(str(LAMP_SHADE_CENTS) in r for r in verdict.reasons)


def test_the_diff_changes_no_schema_and_no_argument(world: World) -> None:
    """Which is why the type checker and the contract test had nothing to say."""
    drifted = build_library(
        world, search_description=SEARCH_ORDERS_DRIFTED
    )
    spec = drifted.registry.spec_for("search_orders")
    assert spec is not None
    assert spec.input_schema == SEARCH_ORDERS.input_schema
    assert spec.output_schema == SEARCH_ORDERS.output_schema
    assert spec.description != SEARCH_ORDERS.description
    assert spec.version != SEARCH_ORDERS.version


def test_the_two_sentences_the_diff_deleted_are_what_matters() -> None:
    """Partial rows, and what to call next. Either one missing is enough."""
    assert description_is_complete(SEARCH_ORDERS)
    stripped = ToolSpec(
        name="search_orders",
        description=SEARCH_ORDERS_DRIFTED,
        input_schema=SEARCH_ORDERS.input_schema,
        output_schema=SEARCH_ORDERS.output_schema,
        writes=False,
        idempotent=True,
    )
    assert not description_is_complete(stripped)


def test_the_unit_test_on_search_orders_passes_either_way(
    world: World,
) -> None:
    """Everything a normal review looks at was green. This is why."""
    shaped = build_library(world)
    drifted = build_library(
        World(), search_description=SEARCH_ORDERS_DRIFTED
    )
    call = ToolCall("c1", "search_orders", {"customer_id": "CUST-8841"})
    before = shaped.registry.dispatch(call, run_id=RUN_ID, step=1)
    after = drifted.registry.dispatch(call, run_id=RUN_ID, step=1)
    assert before.ok and after.ok
    assert before.content == after.content


# ---------------------------------------------------------------- the demo


def test_the_demo_exits_zero() -> None:
    """The printed command is the tested command."""
    assert demo.main([]) == 0
