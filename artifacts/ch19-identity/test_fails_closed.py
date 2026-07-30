"""Negative capability tests: what this agent cannot do.

Positive tests prove the happy path. Only an assertion that the agent
*cannot* act catches a scope somebody widened in a config file to unblock
a demo, and that is the assertion this file exists for.

Every test here asserts on behaviour — a decision, a ledger, a raised
refusal — and none of them assert on a message string.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import pytest
from authz_server import (
    AudienceMismatch,
    AuthorizationServer,
    ExpiredToken,
    InsufficientScope,
)
from broker import TOOL_AUTHORITY, TokenBroker
from claims import REQUIRED_CLAIMS, missing_claims
from northstar_contracts import ToolCall, idempotency_key
from northstar_policy import Decision, Principal
from policy import APPROVAL_THRESHOLD_CENTS, gateway_policy, policy
from run_refund import AMOUNT, ORDER, RUN_ID, USER, refund_call, run_refund

FULL_GRANT = frozenset({"orders.read", "refunds.write"})
WITHHELD_GRANT = frozenset({"orders.read"})

REFUND_AUDIENCE, REFUND_SCOPE = TOOL_AUTHORITY["issue_refund"]
MESSAGE_AUDIENCE, MESSAGE_SCOPE = TOOL_AUTHORITY["send_message"]


def a_clock(start: float = 1000.0) -> tuple[list[float], object]:
    """A clock a test can wind forward, so expiry needs no sleep."""
    now = [start]
    return now, (lambda: now[0])


# ------------------------------------------------------- the decision point


def test_refund_without_scope_is_denied() -> None:
    """The chapter's assertion, on the policy object the gateway uses."""
    caller = Principal.of("CUST-8841", "orders.read")
    call = ToolCall(
        id="call_1",
        name="issue_refund",
        arguments={
            "order_id": ORDER,
            "amount_cents": AMOUNT,
            "idempotency_key": idempotency_key(RUN_ID, "refund"),
        },
    )
    assert policy.evaluate(caller, call, {}) is Decision.DENY
    assert gateway_policy().evaluate(caller, call, {}) is Decision.DENY


def test_the_scope_rule_is_what_refuses_not_the_default() -> None:
    """A fallthrough would deny too, and would deny for the wrong reason.

    The rule name is what an incident review reads, so a denial that
    lands on ``default`` is a denial nobody can explain.
    """
    caller = Principal.of("CUST-8841", "orders.read")
    verdict = gateway_policy().evaluate_verbose(caller, refund_call(), {})
    assert verdict.decision is Decision.DENY
    assert verdict.rule == "issue_refund.requires.refunds.write"


def test_a_refund_at_exactly_the_threshold_needs_a_human() -> None:
    """``>=``, not ``>``. A line you can sit on is a line someone sits on."""
    caller = Principal(user_id=USER, scopes=FULL_GRANT)
    engine = gateway_policy()
    call = refund_call()
    at = ToolCall(
        call.id,
        call.name,
        {**call.arguments, "amount_cents": APPROVAL_THRESHOLD_CENTS},
    )
    under = ToolCall(
        call.id,
        call.name,
        {**call.arguments, "amount_cents": APPROVAL_THRESHOLD_CENTS - 1},
    )
    assert engine.evaluate(caller, at, {}) is Decision.REQUIRE_APPROVAL
    assert engine.evaluate(caller, under, {}) is Decision.ALLOW


def test_an_unknown_write_falls_through_to_deny() -> None:
    """Deny by default means a tool nobody wrote a rule for is refused."""
    caller = Principal(user_id=USER, scopes=FULL_GRANT)
    call = ToolCall("c1", "wire_transfer", {"amount_cents": 1})
    assert gateway_policy().evaluate(caller, call, {}) is Decision.DENY


# ------------------------------------------------------------- the credential


def test_a_token_minted_for_refunds_is_refused_by_messages() -> None:
    """The confused deputy, and the claim that stops it."""
    server = AuthorizationServer(grants={f"user:{USER}": FULL_GRANT})
    token = server.sign(
        sub=USER,
        act={"sub": "northstar-support-agent@v1.8.0"},
        aud=REFUND_AUDIENCE,
        scope=REFUND_SCOPE,
        resource=f"order:{ORDER}",
    )
    # The service it was minted for accepts it.
    assert server.verify(token, audience=REFUND_AUDIENCE, scope=REFUND_SCOPE)
    # Every other service does not.
    with pytest.raises(AudienceMismatch):
        server.verify(
            token, audience=MESSAGE_AUDIENCE, scope=MESSAGE_SCOPE
        )


def test_a_token_past_its_sixty_seconds_is_refused() -> None:
    """Short-lived means the credential in a leaked checkpoint is inert."""
    now, clock = a_clock()
    server = AuthorizationServer(
        grants={f"user:{USER}": FULL_GRANT}, clock=clock  # type: ignore[arg-type]
    )
    token = server.sign(
        sub=USER,
        act={"sub": "northstar-support-agent@v1.8.0"},
        aud=REFUND_AUDIENCE,
        scope=REFUND_SCOPE,
        resource=f"order:{ORDER}",
        ttl_s=60,
    )
    assert server.verify(token, audience=REFUND_AUDIENCE, scope=REFUND_SCOPE)

    now[0] += 61.0
    with pytest.raises(ExpiredToken):
        server.verify(token, audience=REFUND_AUDIENCE, scope=REFUND_SCOPE)


def test_the_exchange_will_not_mint_a_scope_the_user_does_not_hold() -> None:
    """Step up, never widen. The refusal is the whole control."""
    server = AuthorizationServer(grants={f"user:{USER}": WITHHELD_GRANT})
    broker = TokenBroker(server)
    with pytest.raises(InsufficientScope):
        broker.for_call(
            Principal(user_id=USER, scopes=WITHHELD_GRANT), refund_call()
        )


def test_every_minted_token_carries_both_parties() -> None:
    """``sub`` names the user, ``act`` names the workload. Both, always."""
    run = run_refund(grant=FULL_GRANT)
    assert run.server.issued
    for token in run.server.issued:
        assert missing_claims(token.claims) == []
        assert set(REQUIRED_CLAIMS) <= set(token.claims)
        assert token.subject == USER
        assert token.actor.startswith("northstar-support-agent@")


# ------------------------------------------------------------- the whole run


def test_the_granted_run_refunds_exactly_once() -> None:
    """The positive control, so the negative ones mean something."""
    run = run_refund(grant=FULL_GRANT)
    assert run.state.status == "succeeded"
    assert len(run.world.refunds_for(ORDER)) == 1
    assert run.refunded_cents == AMOUNT


def test_removing_one_scope_stops_the_money() -> None:
    """One entry in a mapping the agent cannot see. Nothing else changes."""
    granted = run_refund(grant=FULL_GRANT)
    withheld = run_refund(grant=WITHHELD_GRANT)

    assert granted.refunded_cents == AMOUNT
    assert withheld.refunded_cents == 0
    assert withheld.world.ledger == []
    # The transcript is identical. Only the world tells them apart, which
    # is why the grader reads the world.
    assert granted.final_text == withheld.final_text


def test_a_denied_call_never_causes_a_credential_to_exist() -> None:
    """Policy first, broker second. The order is the property."""
    withheld = run_refund(grant=WITHHELD_GRANT)
    minted_scopes = {t.scope for t in withheld.server.issued}
    assert "refunds.write" not in minted_scopes
    assert not [
        e for e in withheld.broker.exchanges if e["tool"] == "issue_refund"
    ]


def test_the_denial_reaches_the_model_as_permanent() -> None:
    """A retryable denial sends the agent round the loop against a wall."""
    withheld = run_refund(grant=WITHHELD_GRANT)
    refusals = [
        m.content
        for m in withheld.state.messages
        if m.role == "tool"
        and isinstance(m.content, dict)
        and m.content.get("ok") is False
    ]
    assert refusals
    for refusal in refusals:
        assert refusal["content"]["retryable"] is False


def test_every_decision_is_attributable() -> None:
    """Eight fields cheap to write now, impossible to reconstruct later."""
    run = run_refund(grant=FULL_GRANT)
    records = run.decision_records
    assert len(records) == 3  # get_order, get_policy, issue_refund
    for record in records:
        payload = record["payload"]
        assert payload["user_id"] == USER
        assert payload["agent_id"] == "northstar-support-agent"
        assert payload["operator_id"] == "northstar-platform"
        assert payload["decision"] in {d.value for d in Decision}
        assert payload["rule"] and payload["rule"] != "default"
