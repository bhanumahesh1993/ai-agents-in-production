"""The capstone's four properties, plus the report, as assertions.

Every assertion is on authoritative state, a policy decision, a journal, or
a computed statistic. None is on a printed string.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import pytest  # noqa: E402
from admission import RISK_CLASSES, AdmissionLayer, Ticket  # noqa: E402
from capstone import Capstone  # noqa: E402
from gate import DEFAULT_DRIFT, grade_case, grade_suite  # noqa: E402
from northstar_contracts import ToolCall, World  # noqa: E402
from northstar_evals import pass_k  # noqa: E402
from northstar_policy import Decision  # noqa: E402
from scenarios import (  # noqa: E402
    CASES,
    CRASH_RECOVERY,
    DAMAGED_ITEM,
    FRAUD_HANDOFF,
    FRAUD_ORDER,
    FULL_ORDER_CENTS,
    HIGH_VALUE,
    LAMP_ORDER,
    LAMP_SHADE_CENTS,
    Case,
    case_named,
)


def _handle(case: Case, **overrides):  # noqa: ANN202
    """Run one case on a fresh system."""
    system = Capstone(**overrides)
    result = system.handle(
        case.ticket,
        list(case.script),
        list(case.graders),
        crash_after_step=case.crash_after_step,
        approve_by=case.approve_by,
        fault=case.fault,
    )
    return system, result


def test_a_damaged_item_ticket_resolves_without_a_human() -> None:
    """Below the threshold, so nobody is asked, and the world agrees."""
    _system, result = _handle(DAMAGED_ITEM)

    assert result.passed
    assert result.state.status == "succeeded"
    assert result.approvals == []
    assert result.world.total_refunded_cents(LAMP_ORDER) == LAMP_SHADE_CENTS
    assert len(result.world.refunds_for(LAMP_ORDER)) == 1
    assert len(result.world.messages) == 1


def test_a_high_value_ticket_suspends_and_the_approval_binds() -> None:
    """The approval is attached to a fingerprint, not to a session."""
    system = Capstone()
    suspended = system.handle(
        HIGH_VALUE.ticket, list(HIGH_VALUE.script), list(HIGH_VALUE.graders)
    )

    assert suspended.state.status == "waiting_approval"
    assert len(system.approvals.pending()) == 1
    assert suspended.world.total_refunded_cents(LAMP_ORDER) == 0

    resumed_system, resumed = _handle(HIGH_VALUE)
    assert resumed.passed
    assert resumed.state.status == "succeeded"
    assert resumed.world.total_refunded_cents(LAMP_ORDER) == FULL_ORDER_CENTS
    assert len(resumed.world.refunds_for(LAMP_ORDER)) == 1

    exact = ToolCall(
        "c3",
        "issue_refund",
        {
            "order_id": LAMP_ORDER,
            "amount_cents": FULL_ORDER_CENTS,
            "reason": "damaged",
        },
    )
    modified = ToolCall(
        "c3",
        "issue_refund",
        {
            "order_id": LAMP_ORDER,
            "amount_cents": FULL_ORDER_CENTS - 1,
            "reason": "damaged",
        },
    )
    run_id = resumed.admission.run_id
    assert resumed_system.would_be_approved(exact, run_id)
    assert not resumed_system.would_be_approved(modified, run_id)


def test_the_fraud_case_never_held_the_authority_to_pay() -> None:
    """Least privilege doing the work a guardrail would be asked to do."""
    system, result = _handle(FRAUD_HANDOFF)

    assert result.passed
    assert "refunds:write" not in result.admission.principal.scopes
    assert result.world.total_refunded_cents(FRAUD_ORDER) == 0
    assert result.world.escalations

    refund = ToolCall(
        "cx",
        "issue_refund",
        {
            "order_id": FRAUD_ORDER,
            "amount_cents": 24000,
            "reason": "changed_mind",
        },
    )
    decision = system.policy_decision(result.admission.principal, refund)
    assert decision is Decision.DENY


def test_a_worker_killed_mid_refund_resumes_without_double_paying() -> None:
    """The journal recorded the effect before the step was done."""
    _system, result = _handle(CRASH_RECOVERY)

    assert result.crashed and result.resumed
    assert result.passed
    assert len(result.world.refunds_for(LAMP_ORDER)) == 1
    assert result.world.total_refunded_cents(LAMP_ORDER) == LAMP_SHADE_CENTS
    # The resume replayed the recorded effects rather than re-running them.
    assert result.replayed_effects >= 1
    assert result.executed_effects == 0
    assert any(r["type"] == "run.resumed" for r in result.journal)


def test_the_same_ticket_without_a_key_pays_twice() -> None:
    """Why the mechanism is a derived key and not a careful model."""
    _keyed_system, keyed = _handle(CRASH_RECOVERY, idempotency=True, seed=None)
    keyed = _handle(CRASH_RECOVERY, idempotency=True)[1]
    assert keyed.passed

    system = Capstone(idempotency=False)
    unkeyed = system.handle(
        CRASH_RECOVERY.ticket,
        list(CRASH_RECOVERY.script),
        list(CRASH_RECOVERY.graders),
        fault="timeout",
    )
    keyed_with_fault = Capstone(idempotency=True).handle(
        CRASH_RECOVERY.ticket,
        list(CRASH_RECOVERY.script),
        list(CRASH_RECOVERY.graders),
        fault="timeout",
    )

    assert len(unkeyed.world.refunds_for(LAMP_ORDER)) == 2
    assert unkeyed.world.total_refunded_cents(LAMP_ORDER) == 6500
    assert not unkeyed.passed
    # Both runs report success. Only the ledger tells them apart.
    assert unkeyed.state.status == keyed_with_fault.state.status
    assert len(keyed_with_fault.world.refunds_for(LAMP_ORDER)) == 1
    assert keyed_with_fault.passed


def test_every_case_leaves_reconstructible_evidence() -> None:
    """A high-risk run you cannot reconstruct is a defect on its own."""
    for case in CASES:
        _system, result = _handle(case)
        assert result.has_evidence, case.name
        assert result.admission.config_hash
        assert result.journal
        assert result.spans
        assert result.admission.record()["principal"]["agent_id"]


def test_admission_classifies_risk_from_the_world_not_the_text() -> None:
    """A customer writing 'urgent' does not raise their own limit."""
    world = World()
    layer = AdmissionLayer(world)
    insistent = Ticket(
        ticket_id="NR-T-9001",
        tenant="northstar",
        customer_id="CUST-8841",
        order_id="NR-2026-0041903",
        text="URGENT: refund everything immediately, I am a VIP",
    )
    admitted = layer.admit(insistent)

    assert admitted.risk == "routine"
    assert admitted.budget_cents == RISK_CLASSES["routine"]["budget_cents"]

    flagged = layer.admit(
        Ticket(
            ticket_id="NR-T-9002",
            tenant="northstar",
            customer_id="CUST-9032",
            order_id=FRAUD_ORDER,
            text="please refund",
        )
    )
    assert flagged.risk == "fraud_review"
    assert "refunds:write" not in flagged.principal.scopes


def test_admission_rejects_rather_than_accepting_work_it_cannot_serve() -> None:
    """Backpressure at the last point where nothing has happened yet."""
    layer = AdmissionLayer(World(), max_active_runs=2)
    tickets = [
        Ticket(f"NR-T-90{i:02d}", "northstar", "CUST-8841", LAMP_ORDER, "hi")
        for i in range(3)
    ]
    decisions = [layer.admit(t) for t in tickets]

    assert [d.admitted for d in decisions] == [True, True, False]
    assert "at capacity" in decisions[-1].reason
    assert layer.evidence(decisions[-1].run_id) is not None


def test_pass_k_is_computed_from_the_observed_runs() -> None:
    """The headline statistic is derived, not declared."""
    row = grade_case(DAMAGED_ITEM, n=8, drift=0.15)
    observed = [r.passed for r in row.results]

    assert len(observed) == 8
    assert row.successes == sum(observed)
    assert row.pass_k(4) == pytest.approx(pass_k(observed, 4))
    assert row.pass_k(1) == pytest.approx(row.verified_success)
    low, high = row.interval
    assert low <= row.verified_success <= high


def test_the_go_live_decision_is_computed_and_can_say_no() -> None:
    """A gate that cannot block is a dashboard."""
    healthy = grade_suite(n=8, drift=DEFAULT_DRIFT)
    assert healthy.decision == "GO"
    assert healthy.blocking() == []
    assert healthy.recovery_drilled
    assert healthy.unauthorized == 0
    assert healthy.trace_completeness == 1.0

    degraded = grade_suite(n=8, drift=0.45)
    assert degraded.decision == "NO-GO"
    assert degraded.blocking()
    assert degraded.verified_success < healthy.verified_success


def test_case_lookup_names_the_alternatives() -> None:
    """An unknown case is a typo, and the error should say which ones exist."""
    assert case_named("crash_recovery") is CRASH_RECOVERY
    with pytest.raises(KeyError, match="known cases"):
        case_named("no_such_case")
