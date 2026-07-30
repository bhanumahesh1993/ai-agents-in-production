# Chapter 19 — a refund under a token that names both parties

**What this artifact proves:** an engineer, reviewer, or auditor can read one
event record and say which customer a refund was for, which agent version
issued it, which team owns that agent, which rule allowed it, and which
credential to revoke — and removing one scope from one mapping the agent cannot
see stops the refund happening at all, rather than merely discouraging it.

## Run it

```bash
make demo-ch19
# or
python artifacts/ch19-identity/demo.py
```

The demo runs the same trajectory twice against the same world:

1. **Granted.** `refunds.write` is in the user's grant. The broker exchanges the
   user's token for a 60-second credential naming one audience, one scope, and
   one order. The demo prints the decoded claims and the decision event. Ledger:
   one refund, 3,250 cents.
2. **Withheld.** One entry removed from the grant. Nothing else changes — same
   prompt, same model, same tools, same code. The decision point denies on
   `issue_refund.requires.refunds.write`, no credential is ever minted, and the
   ledger stays empty.

It then asks the same policy object about a refund at exactly 5,000 cents
(`require_approval`, not `allow`) and presents a refund token to the message
service (refused on audience mismatch).

The demo exits non-zero if the granted run does not refund exactly once, if the
withheld run moves any money, if the denial lands on the default rule rather
than the scope rule, if a denied call still caused a credential to exist, or if
the message service accepts a token minted for refunds.

## Files

| File | What it is |
|---|---|
| `claims.py` | What a delegated token's claims look like decoded, and the seven a credential must carry for the audit trail to answer "who was this for, and which build did it". |
| `authz_server.py` | A fake authorization server implementing RFC 8693 token exchange offline. Scope, audience, expiry, and the `sub`/`act` pair, with the cryptography replaced by a dictionary because none of those properties are cryptographic. |
| `broker.py` | The just-in-time credential broker, and `TOOL_AUTHORITY`: which audience and which scope each Northstar tool needs. This is the table that turns `refunds.*` into `refunds.write` against exactly one service. |
| `gateway.py` | The policy enforcement point. `DecisionLog` wraps the engine so nothing can decide without recording; `TokenBoundTools` verifies the audience in the *receiver*; `ToolGateway.dispatch` is the one path a governed call takes. |
| `policy.py` | The decision point as data: two refusal rules, two allow rules, deny by default. |
| `run_refund.py` | The whole run, parameterised by the user's grant. Returns a `RefundRun` holding the world, the state, the decision log, and the tokens. |
| `demo.py` | Runs it granted and withheld, prints the claims and the decision, and asserts the difference. |
| `test_fails_closed.py` | Thirteen assertions, all on behaviour. The negative capability tests are the artifact's real output. |
| `conftest.py` | Makes `import policy` mean *this* chapter's policy when all of `artifacts/` runs under one pytest. |

## Read `gateway.py` first

Two lines in `ToolGateway.dispatch` carry the chapter:

```python
if self.policy.evaluate(principal, call, ctx) is not Decision.ALLOW:
    return ToolResult(call.id, ok=False, content={
        "error": "not_authorized", "retryable": False,
    })
token = self.broker.for_call(principal, call)  # scoped, 60s
```

The denial reports `retryable=False`, so the model observes a permanent refusal
instead of spending its remaining turns retrying a wall. And the token is
fetched *after* the decision, so a denied call never causes a credential to
exist — which is why `test_a_denied_call_never_causes_a_credential_to_exist`
asserts on the broker's exchange log and not on the ledger.

## Two places the code deviates from the obvious

`policy.py` carries an allow rule for refunds (`issue_refund.within_autonomy`)
and it is deliberately **last** in the list. The chapter's printed excerpt is
`policy`, the two refusal rules with `default=DENY`; on its own that engine
refuses every refund, including the legitimate one, which is correct for a
decision point in front of money and useless as a gateway bundle. `gateway_policy()`
composes the printed rules with the allow rules the gateway needs, in an order
where both refusals get to fire first. Put the allow rule earlier and a
principal holding nothing gets a refund; the order is asserted in the tests
rather than trusted.

`AuthorizationServer` mints an opaque handle rather than a JWT. An artifact that
shipped a signing key would be shipping a secret, and every property the chapter
cares about lives in the claims rather than in the signature. Its clock is a
counter that advances one second per read, so the expiry test needs no sleep and
the demo's output is stable.
