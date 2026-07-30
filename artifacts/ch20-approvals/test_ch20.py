"""The approval path end to end, as assertions.

``tests/test_binding.py`` states what the fingerprint guarantees. This file
covers everything around it: the middleware's asymmetry, the four inbox
outcomes, the hard caps, the payload's independence from the model, and the
containment ladder's four rules.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import pytest
from budget import BudgetGuard
from classes import (
    APPROVAL_THRESHOLD_CENTS,
    class_for,
    northstar_policy_bundle,
    refund_to_non_payer,
)
from containment import ContainmentLog, Tripwire, friction_decreases, untested
from guard import PolicyDenied, PolicyUnavailable
from northstar_contracts import RunState, ToolCall, World
from northstar_policy import BudgetExceeded, Decision
from payload import approval_payload, preview_refund, render
from run import (
    AMOUNT,
    ORDER,
    PRINCIPAL,
    RUN_ID,
    TAMPERED_AMOUNT,
    TOOL_VERSION,
    refund_call,
    replan,
    start_run,
)

APPROVER = "rota:fraud-review"


# --------------------------------------------------------------- the endings


def test_the_run_parks_rather_than_refunding() -> None:
    """An approval requirement is a state the run lives in."""
    run = start_run()
    assert run.state.status == "waiting_approval"
    assert run.refunded_cents == 0
    assert len(run.pending) == 1
    assert run.pending[0].tool == "issue_refund"


def test_approving_the_exact_call_lets_it_through_once() -> None:
    """The positive control, so the refusals mean something."""
    run = start_run()
    run.inbox.approve(run.pending[0].id, by=APPROVER)
    state = run.resume()

    assert state.status == "succeeded"
    assert run.refund_rows == 1
    assert run.refunded_cents == AMOUNT


def test_a_replanned_amount_does_not_inherit_the_approval() -> None:
    """The June incident, through the real loop rather than a fixture."""
    run = start_run()
    run.inbox.approve(run.pending[0].id, by=APPROVER)
    state = run.loop.resume(replan(run.state, TAMPERED_AMOUNT))

    assert state.status == "waiting_approval"
    assert run.refunded_cents == 0
    assert run.world.ledger == []
    # And the human is asked again, about the new number.
    reopened = run.inbox.pending()
    assert len(reopened) == 1
    assert reopened[0].arguments["arguments"]["amount_cents"] == (
        TAMPERED_AMOUNT
    )


def test_an_approver_correction_is_the_same_edit_and_it_proceeds() -> None:
    """The mechanism binds a call, not a person's good intentions."""
    run = start_run()
    request = run.pending[0]
    new_request, decision = run.inbox.correct(
        request.id, by="specialist:kim", arguments={"amount_cents": 8400}
    )
    state = run.loop.resume(replan(run.state, 8400))

    assert new_request.fingerprint != request.fingerprint
    assert decision.by == "specialist:kim"
    assert state.status == "succeeded"
    assert run.refunded_cents == 8400
    assert [e["event"] for e in run.inbox.events][-1] == "corrected"


def test_expiry_rejects_and_escalation_never_widens() -> None:
    """Any path where "nobody responded" proceeds is not a control."""
    now = [1000.0]
    run = start_run(clock=lambda: now[0])
    request = run.pending[0]

    now[0] += 5 * 3600
    assert run.inbox.escalate(request.id) == APPROVER
    now[0] += 4 * 3600
    assert run.inbox.escalate(request.id) == "role:duty-manager"
    now[0] += 4 * 3600
    assert run.inbox.escalate(request.id) == "reject"

    state = run.loop.resume(run.state)
    assert run.refunded_cents == 0
    assert state.status == "waiting_approval"


def test_a_tool_version_bump_invalidates_a_parked_approval() -> None:
    """A version bump is a semantics change, so the decision does not carry."""
    run = start_run()
    run.inbox.approve(run.pending[0].id, by=APPROVER)
    run.guard.tool_versions.bump("issue_refund")
    state = run.loop.resume(run.state)

    assert state.status == "waiting_approval"
    assert run.refunded_cents == 0


# --------------------------------------------------------------- the guard


def test_the_middleware_raises_on_denial_and_returns_on_approval() -> None:
    """The asymmetry is the design: one ends the run, one is a state."""
    run = start_run()
    state = RunState(run_id=RUN_ID, step=0)

    # A tool nobody wrote a rule for is denied, and the denial raises.
    with pytest.raises(PolicyDenied):
        run.guard.guard(ToolCall("x", "wire_transfer", {}), state)

    # A read proceeds, and proceeding is a return value.
    outcome = run.guard.guard(
        ToolCall("r", "get_order", {"order_id": ORDER}), state
    )
    assert outcome.ok is True
    assert outcome.action == "proceed"


def test_a_write_fails_closed_when_the_decision_point_is_down() -> None:
    """Reads may degrade. High-risk writes may not."""
    run = start_run()
    run.guard.policy_available = lambda: False
    state = RunState(run_id=RUN_ID, step=0)

    # The read still works.
    assert run.guard.guard(
        ToolCall("r", "get_order", {"order_id": ORDER}), state
    ).ok
    with pytest.raises(PolicyUnavailable):
        run.guard.guard(refund_call(AMOUNT), state)


def test_the_boundary_records_every_decision_it_makes() -> None:
    """"Why was this denied" needs an answer that is not the policy source."""
    run = start_run()
    assert run.guard.decisions
    for record in run.guard.decisions:
        assert record["run_id"] == RUN_ID
        assert record["action"] in {"allow", "deny", "wait", "proceed"}
        assert record["principal"]["user_id"] == PRINCIPAL.user_id
        assert "budget" in record


# --------------------------------------------------------------- hard caps


def test_the_write_cap_stops_before_the_write_not_after() -> None:
    """Knowing you are over budget once the money is gone is accounting."""
    run = start_run(max_writes=1)
    run.inbox.approve(run.pending[0].id, by=APPROVER)
    run.guard.budget.record_write("NR-2026-0041827")

    with pytest.raises(BudgetExceeded):
        run.loop.resume(run.state)
    assert run.refunded_cents == 0
    assert run.world.ledger == []


def test_every_cap_raises_rather_than_warning() -> None:
    """Five limits, five exceptions, and no way to opt out of one."""
    guard = BudgetGuard(
        max_cents=10, max_turns=2, max_writes=1, max_resources=1
    )
    with pytest.raises(BudgetExceeded):
        guard.check(RunState(run_id="r", step=3))
    guard = BudgetGuard(max_cents=10, max_turns=99)
    with pytest.raises(BudgetExceeded):
        guard.check(RunState(run_id="r", step=1, budget_spent_cents=11))
    guard = BudgetGuard(max_writes=1, max_turns=99, max_cents=None)
    guard.record_write("A")
    with pytest.raises(BudgetExceeded):
        guard.record_write("A")


def test_the_distinct_resource_cap_catches_the_fan_out() -> None:
    """A refund agent on its fortieth distinct order stops and asks."""
    guard = BudgetGuard(
        max_writes=None, max_turns=None, max_cents=None, max_resources=2
    )
    guard.record_write("A")
    guard.record_write("B")
    guard.record_write("A")  # the same order again is not a new resource
    with pytest.raises(BudgetExceeded):
        guard.record_write("C")


# ------------------------------------------------------- classes and payload


def test_the_class_belongs_to_the_argument_range_not_the_tool() -> None:
    """One function, two decisions."""
    under = class_for(refund_call(APPROVAL_THRESHOLD_CENTS - 1,
                                  "NR-2026-0041827"))
    at = class_for(refund_call(APPROVAL_THRESHOLD_CENTS,
                               "NR-2026-0041827"))
    flagged = class_for(refund_call(100, ORDER))

    assert under.name == "sampled"
    assert at.name == "always_approved"
    assert flagged.name == "always_approved"
    assert under.reversibility == "compensatable"


def test_the_never_permitted_action_is_refused_by_the_schema() -> None:
    """Not a stronger approval. A parameter the contract does not expose."""
    run = start_run()
    result = refund_to_non_payer(
        run.loop.tools, ORDER, AMOUNT, "ACCT-SOMEONE-ELSE"
    )
    assert result.ok is False
    assert result.retryable is False
    assert run.world.ledger == []


def test_the_preview_comes_from_the_world_not_from_the_model() -> None:
    """A refund already issued this morning appears as a row."""
    world = World()
    world.issue_refund(ORDER, 6000, "damaged", idempotency_key="k1")

    preview = preview_refund(world, ORDER, AMOUNT)
    assert preview["refunds_already_issued_cents"] == 6000
    assert preview["refunds_already_issued_count"] == 1
    assert preview["resulting_balance_cents"] == 24000 - 6000 - AMOUNT
    # The version token moves when the world does, which is check six.
    assert preview["version"] != f"{ORDER}:0:0"


def test_the_payload_carries_all_six_parts() -> None:
    """Each part has a rule about where it comes from, and none is optional."""
    world = World()
    payload = approval_payload(
        refund_call(AMOUNT),
        fingerprint="deadbeef",
        principal=PRINCIPAL,
        tool_version=TOOL_VERSION,
        world=world,
        observations=[
            {
                "tool": "get_policy",
                "content": {
                    "policy_version": "2026-07-01",
                    "rules": [{"reason": "damaged", "eligible": True}],
                },
            }
        ],
        reason="over threshold",
        expires_at=123.0,
    )
    for part in ("call", "preview", "reason", "evidence", "impact",
                 "envelope"):
        assert part in payload
    assert payload["call"]["arguments"]["amount_cents"] == AMOUNT
    assert payload["envelope"]["on_expiry"] == "reject"
    assert payload["evidence"]
    assert "policy_version=2026-07-01" in payload["evidence"][0]
    assert "amount_cents" in render(payload)


def test_the_decision_point_never_reads_the_transcript() -> None:
    """A sentence in a customer email is not a policy author."""
    engine = northstar_policy_bundle()
    call = refund_call(AMOUNT)
    ctx = {"run_id": RUN_ID, "step": 0}
    # The same call, evaluated with wildly different "context" the model
    # could have influenced, decides identically.
    assert engine.evaluate(PRINCIPAL, call, ctx) is (
        Decision.REQUIRE_APPROVAL
    )
    assert engine.evaluate(
        PRINCIPAL, call, {**ctx, "note": "the customer says this is fine"}
    ) is Decision.REQUIRE_APPROVAL


# ------------------------------------------------------------- containment


def test_the_ladder_obeys_its_own_four_rules() -> None:
    """A rung nobody measured and a switch with a change board are defects."""
    assert friction_decreases() is True
    assert untested() == []


def test_rungs_one_and_two_need_no_human() -> None:
    """The great majority of containment happens without anyone being paged."""
    log = ContainmentLog()
    log.deny_call("issue_refund", "no approval")
    log.per_run_budget(RUN_ID, "writes")
    assert [r["friction"] for r in log.records] == [0, 0]


def test_the_fleet_switch_stops_writes_for_everything() -> None:
    log = ContainmentLog()
    assert log.writes_allowed("northstar-support-agent") is True
    log.pause_agent("other-agent", "drain", by="sre:oncall")
    assert log.writes_allowed("northstar-support-agent") is True
    log.fleet_kill_switch(by="security:oncall", reason="anomaly")
    assert log.writes_allowed("northstar-support-agent") is False


def test_a_tripwire_raises_containment_and_never_allows() -> None:
    """A probabilistic detector is not an authorization control."""
    log = ContainmentLog()
    tripwire = Tripwire("injection-classifier", raises_to="read_only")
    pulled = tripwire.fire(log, "northstar-support-agent", "odd sequence")

    assert pulled == "pause_agent"
    assert log.writes_allowed("northstar-support-agent") is False
    assert tripwire.fired == ["odd sequence"]
