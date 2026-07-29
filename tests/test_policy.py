"""Policy, approvals, and budgets — the three things the model cannot vote on."""

from __future__ import annotations

import pytest
from conftest import DAMAGED_ORDER, DELIVERED_ORDER, FLAGGED_ORDER
from northstar_contracts import ToolCall, World
from northstar_policy import (
    ApprovalStore,
    BudgetExceeded,
    BudgetGuard,
    Decision,
    Principal,
    RulesPolicyEngine,
    TurnLimitExceeded,
    approval_fingerprint,
    default_northstar_policy,
    deny_tool,
)
from northstar_runtime import AgentLoop, FakeModel, PolicyDenied

AGENT_SCOPES = ("orders:read", "refunds:write")


def refund_call(cents: int, order_id: str = DELIVERED_ORDER) -> ToolCall:
    """A refund of a given size."""
    return ToolCall(
        "c1",
        "issue_refund",
        {
            "order_id": order_id,
            "amount_cents": cents,
            "reason": "damaged",
        },
    )


# ---------------------------------------------------------------- decisions


def test_small_refund_is_allowed() -> None:
    """Below the threshold, the agent acts on its own."""
    policy = default_northstar_policy()
    principal = Principal.of("CUST-8841", *AGENT_SCOPES)

    assert policy.evaluate(principal, refund_call(3250), {}) is Decision.ALLOW


def test_large_refund_needs_a_human() -> None:
    """At or above the threshold, a human decides. Note: at, not above."""
    policy = default_northstar_policy()
    principal = Principal.of("CUST-8841", *AGENT_SCOPES)

    assert (
        policy.evaluate(principal, refund_call(5000), {})
        is Decision.REQUIRE_APPROVAL
    )
    assert (
        policy.evaluate(principal, refund_call(8400), {})
        is Decision.REQUIRE_APPROVAL
    )


def test_missing_scope_denies_regardless_of_amount() -> None:
    """No scope, no refund. Nothing in the transcript changes that."""
    policy = default_northstar_policy()
    principal = Principal.of("CUST-8841", "orders:read")

    assert policy.evaluate(principal, refund_call(100), {}) is Decision.DENY


def test_flagged_order_always_needs_a_human() -> None:
    """A fraud flag outranks the amount threshold."""
    policy = default_northstar_policy()
    principal = Principal.of("CUST-9032", *AGENT_SCOPES)
    call = refund_call(100, FLAGGED_ORDER)

    assert (
        policy.evaluate(principal, call, {}) is Decision.REQUIRE_APPROVAL
    )


def test_verdict_names_the_rule_that_fired() -> None:
    """An audit record needs the reason, not just the answer."""
    policy = default_northstar_policy()
    principal = Principal.of("CUST-8841", *AGENT_SCOPES)
    verdict = policy.evaluate_verbose(principal, refund_call(8400), {})

    assert verdict.decision is Decision.REQUIRE_APPROVAL
    assert "5000" in verdict.rule
    assert "threshold" in verdict.reason


def test_denial_stops_the_run(world: World) -> None:
    """A denied call raises. The model does not get to try a variation."""
    loop = AgentLoop(
        model=FakeModel(default=[refund_call(1000, DAMAGED_ORDER), "done"]),
        tools=world.tools(),
        policy=RulesPolicyEngine([deny_tool("issue_refund", "read-only run")]),
    )

    with pytest.raises(PolicyDenied, match="issue_refund"):
        loop.run("refund", run_id="run-1")

    assert world.total_refunded_cents(DAMAGED_ORDER) == 0


# ---------------------------------------------------------------- approvals


def test_approval_binds_only_its_own_fingerprint() -> None:
    """Approving 4000 cents does not approve 9900 cents."""
    store = ApprovalStore()
    approved = refund_call(4000)
    request = store.request("run-1", 1, approved, reason="over threshold")
    store.decide(request.id, approved=True, by="ops@northstar")

    assert store.is_approved(approved, run_id="run-1")
    assert not store.is_approved(refund_call(9900), run_id="run-1")
    assert store.status(refund_call(9900), run_id="run-1") == "none"


def test_approval_binds_to_its_run() -> None:
    """A yes for one run is not a yes for the next one."""
    store = ApprovalStore()
    call = refund_call(6000)
    request = store.request("run-1", 1, call)
    store.decide(request.id, approved=True, by="ops@northstar")

    assert store.is_approved(call, run_id="run-1")
    assert not store.is_approved(call, run_id="run-2")


def test_fingerprint_ignores_the_call_id() -> None:
    """Call ids change on replay; an approval must survive that."""
    first = ToolCall("c1", "issue_refund", {"amount_cents": 4000})
    replayed = ToolCall("c9", "issue_refund", {"amount_cents": 4000})

    assert approval_fingerprint(first) == approval_fingerprint(replayed)


def test_denied_approval_does_not_allow() -> None:
    """No is a decision too, and it sticks."""
    store = ApprovalStore()
    call = refund_call(8400)
    request = store.request("run-1", 1, call)
    store.decide(request.id, approved=False, by="ops@northstar")

    assert store.status(call, run_id="run-1") == "denied"
    assert not store.is_approved(call, run_id="run-1")


def test_approvals_expire() -> None:
    """A yes from four hours ago is not consent to act now."""
    now = [1000.0]
    store = ApprovalStore(clock=lambda: now[0], default_ttl_seconds=60)
    call = refund_call(8400)
    request = store.request("run-1", 1, call)
    store.decide(request.id, approved=True, by="ops@northstar")

    assert store.is_approved(call, run_id="run-1")
    now[0] += 61
    assert store.status(call, run_id="run-1") == "expired"
    assert not store.is_approved(call, run_id="run-1")


def test_a_decision_cannot_be_rewritten() -> None:
    """Re-deciding would destroy the audit trail. Open a new request."""
    store = ApprovalStore()
    request = store.request("run-1", 1, refund_call(8400))
    store.decide(request.id, approved=False, by="ops@northstar")

    with pytest.raises(ValueError, match="already decided"):
        store.decide(request.id, approved=True, by="ops@northstar")


def test_repeated_requests_do_not_spam_the_inbox() -> None:
    """A retrying agent must not generate a second question for a human."""
    store = ApprovalStore()
    call = refund_call(8400)
    first = store.request("run-1", 1, call)
    second = store.request("run-1", 2, call)

    assert first.id == second.id
    assert len(store.pending()) == 1


def test_loop_suspends_instead_of_acting(world: World) -> None:
    """The gate holds the write, not just the transcript."""
    store = ApprovalStore()
    loop = AgentLoop(
        model=FakeModel(default=[refund_call(8400), "Refunded in full."]),
        tools=world.tools(),
        policy=default_northstar_policy(),
        approvals=store,
        principal=Principal.of("CUST-8841", *AGENT_SCOPES),
    )
    state = loop.run("refund everything", run_id="run-1")

    assert state.status == "waiting_approval"
    assert world.total_refunded_cents(DELIVERED_ORDER) == 0
    assert len(store.pending()) == 1

    pending = store.pending()[0]
    assert pending.tool == "issue_refund"
    assert pending.arguments["amount_cents"] == 8400

    store.decide(pending.id, approved=True, by="ops@northstar")
    resumed = loop.resume(state)

    assert resumed.status == "succeeded"
    assert world.total_refunded_cents(DELIVERED_ORDER) == 8400


def test_approval_request_shows_the_payload_not_a_summary() -> None:
    """An approver who cannot see the arguments is approving a rumour."""
    store = ApprovalStore()
    request = store.request(
        "run-1",
        1,
        refund_call(8400),
        reason="over threshold",
        principal=Principal.of("CUST-8841", *AGENT_SCOPES),
    )
    payload = request.to_dict()

    assert payload["arguments"]["amount_cents"] == 8400
    assert payload["arguments"]["order_id"] == DELIVERED_ORDER
    assert payload["principal"]["user_id"] == "CUST-8841"
    assert payload["fingerprint"]


# ----------------------------------------------------------------- budgets


def test_budget_guard_counts_all_three_limits() -> None:
    """Money, turns, and wall clock. Any one alone leaves a hole."""
    clock = [0.0]
    guard = BudgetGuard(
        max_cents=100,
        max_turns=3,
        max_wall_seconds=10,
        run_id="run-1",
        clock=lambda: clock[0],
    ).start()

    guard.charge(60)
    assert guard.remaining_cents == 40
    guard.tick()
    assert guard.remaining_turns == 2

    with pytest.raises(BudgetExceeded) as exc:
        guard.charge(50)
    assert exc.value.kind == "cents"


def test_wall_clock_budget_needs_no_sleeping() -> None:
    """An injectable clock keeps time-based tests fast and honest."""
    clock = [0.0]
    guard = BudgetGuard(
        max_cents=None,
        max_turns=None,
        max_wall_seconds=5,
        clock=lambda: clock[0],
    ).start()
    clock[0] = 6.0

    with pytest.raises(BudgetExceeded) as exc:
        guard.check()
    assert exc.value.kind == "wall_clock"


def test_turn_limit_is_a_budget_too() -> None:
    """``except BudgetExceeded`` catches every kind of exhaustion."""
    guard = BudgetGuard(max_cents=None, max_turns=1)
    guard.tick()

    with pytest.raises(TurnLimitExceeded):
        guard.tick()
    assert issubclass(TurnLimitExceeded, BudgetExceeded)


def test_would_exceed_stops_before_the_money_is_gone() -> None:
    """Knowing you are over budget afterwards is accounting, not control."""
    guard = BudgetGuard(max_cents=100, max_turns=None)
    guard.charge(80)

    assert guard.would_exceed(30)
    assert not guard.would_exceed(20)
