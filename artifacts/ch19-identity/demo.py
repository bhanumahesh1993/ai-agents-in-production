"""One Northstar refund under delegated authority, then the same run
with the refund scope withheld.

    python artifacts/ch19-identity/demo.py

Prints the decoded token claims and the decision event for both runs.
Exits non-zero if the granted run does not refund exactly once, if the
withheld run refunds at all, or if the out-of-scope call is refused for
any reason other than the missing scope.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from dataclasses import replace

from authz_server import AudienceMismatch, AuthorizationServer
from broker import TOOL_AUTHORITY
from claims import describe, missing_claims
from northstar_contracts import ToolCall
from northstar_policy import Decision, Principal
from policy import APPROVAL_THRESHOLD_CENTS, gateway_policy
from run_refund import (
    AMOUNT,
    ORDER,
    RUN_ID,
    USER,
    RefundRun,
    caller,
    refund_call,
    run_refund,
)

#: What a support interaction is meant to be able to do.
FULL_GRANT = frozenset({"orders.read", "refunds.write"})

#: The same grant with one entry removed. Nothing else changes between
#: the two runs: same prompt, same model, same tools, same code path.
WITHHELD_GRANT = frozenset({"orders.read"})

#: The eight attribution fields the chapter asks every decision to carry,
#: minus the sub-agent and approver this artifact has neither of.
AUDIT_FIELDS = (
    "user_id",
    "agent_id",
    "operator_id",
    "tool",
    "decision",
    "rule",
    "reason",
    "audience",
    "scope_required",
    "scopes_granted",
)


def caller_with(grant: frozenset[str]) -> Principal:
    """The demo principal, holding exactly ``grant``."""
    return replace(caller, scopes=grant)


def print_claims(run: RefundRun) -> None:
    """Show the credential the refund actually ran under."""
    minted = [t for t in run.server.issued if t.scope == "refunds.write"]
    if not minted:
        print("  (no refund credential was ever minted)")
        return
    token = minted[-1]
    print(f"  token ref     : {token.value}")
    for line in describe(token.claims):
        print(line)
    print(f"  missing claims: {missing_claims(token.claims) or 'none'}")
    print(
        f"  reads as      : a refund for {token.subject}, "
        f"issued by {token.actor}"
    )


def print_decision(run: RefundRun, tool: str) -> None:
    """Show the audit record the decision point produced."""
    record = run.decision_for(tool)
    if record is None:
        print(f"  (no decision was recorded for {tool})")
        return
    payload = record["payload"]
    for field in AUDIT_FIELDS:
        print(f"  {field:<15}: {payload[field]!r}")


def report(label: str, run: RefundRun) -> None:
    """Print one run's credential, decision, and resulting ledger."""
    print(f"\n=== {label} ===")
    print("-- the credential the refund ran under --")
    print_claims(run)
    print("-- the decision event --")
    print_decision(run, "issue_refund")
    print("-- the world, which is the only authority --")
    print(f"  refunds        : {len(run.world.refunds_for(ORDER))}")
    print(f"  refunded cents : {run.refunded_cents}")
    print(f"  run status     : {run.state.status}")
    print(f"  customer read  : {run.final_text!r}")


def audience_check() -> str:
    """Present a refund token to the message service and watch it bounce.

    The audience claim is the control that defeats the confused deputy,
    and the check belongs in the *receiver*, so this is what the message
    service does rather than what the agent promises about itself.

    Returns:
        The refusal message, or an empty string if the token was accepted.
    """
    server = AuthorizationServer(grants={f"user:{USER}": FULL_GRANT})
    refund_audience, refund_scope = TOOL_AUTHORITY["issue_refund"]
    message_audience, message_scope = TOOL_AUTHORITY["send_message"]
    token = server.sign(
        sub=USER,
        act={"sub": "northstar-support-agent@v1.8.0"},
        aud=refund_audience,
        scope=refund_scope,
        resource=f"order:{ORDER}",
    )
    try:
        server.verify(token, audience=message_audience, scope=message_scope)
    except AudienceMismatch as exc:
        return str(exc)
    return ""


def at_threshold_call() -> ToolCall:
    """The same refund, one cent short of nothing: exactly the threshold."""
    call = refund_call("c9")
    return ToolCall(
        call.id,
        call.name,
        {**call.arguments, "amount_cents": APPROVAL_THRESHOLD_CENTS},
    )


def main() -> int:
    print("Chapter 19 — a refund under a token that names both parties")
    print(f"principal : {caller.to_dict()}")
    print(f"order     : {ORDER}, {AMOUNT} cents, run {RUN_ID}")

    granted = run_refund(grant=FULL_GRANT)
    report("granted: refunds.write is in the user's grant", granted)

    withheld = run_refund(grant=WITHHELD_GRANT)
    report("withheld: one entry removed from the grant", withheld)

    print("\n=== the same policy object, asked directly ===")
    verdict = gateway_policy().evaluate_verbose(
        caller_with(FULL_GRANT), at_threshold_call(), {}
    )
    print(
        f"  a {APPROVAL_THRESHOLD_CENTS}c refund -> "
        f"{verdict.decision.value} ({verdict.rule})"
    )

    mismatch = audience_check()
    print("\n=== a refund token presented to the message service ===")
    print(f"  {mismatch or 'ACCEPTED — which would be the bug'}")

    print("\n--- what this proves ---")
    print("One event record names the customer, the agent version, the")
    print("owning team, the rule that decided, and the credential to")
    print("revoke. Removing one scope stops the action rather than")
    print("merely discouraging it.")

    failures: list[str] = []
    if granted.refunded_cents != AMOUNT:
        failures.append(
            f"granted run refunded {granted.refunded_cents}c, want {AMOUNT}c"
        )
    if len(granted.world.refunds_for(ORDER)) != 1:
        failures.append("granted run did not produce exactly one refund")
    if withheld.refunded_cents != 0:
        failures.append(
            f"withheld run moved {withheld.refunded_cents}c; it must move 0"
        )

    denial = withheld.decision_for("issue_refund")
    if denial is None:
        failures.append("withheld run recorded no decision for issue_refund")
    else:
        payload = denial["payload"]
        if payload["decision"] != Decision.DENY.value:
            failures.append(
                f"withheld run decided {payload['decision']!r}, want 'deny'"
            )
        if payload["rule"] != "issue_refund.requires.refunds.write":
            failures.append(
                f"withheld run denied on {payload['rule']!r}; the scope rule "
                f"has to be what refuses, not a fallthrough to the default"
            )

    if any(e["tool"] == "issue_refund" for e in withheld.broker.exchanges):
        failures.append("a denied call still caused a credential to exist")
    if verdict.decision is not Decision.REQUIRE_APPROVAL:
        failures.append(
            f"a {APPROVAL_THRESHOLD_CENTS}c refund decided "
            f"{verdict.decision.value!r}, want 'require_approval'"
        )
    if not mismatch:
        failures.append("the message service accepted a refund token")

    if failures:
        print("\nFAILED:")
        for line in failures:
            print(f"  - {line}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
