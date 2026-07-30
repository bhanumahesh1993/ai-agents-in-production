# Chapter 28 — the capstone, end to end, on a laptop

**What this artifact proves:** every mechanism the book argues for composes into
one runnable system — the loop and harness, context management, typed tools
behind a policy decision point, scoped identity, approvals bound to a call
fingerprint, durable execution with a journal, OpenTelemetry-shaped
instrumentation with a cost ledger, and the reliability, trajectory, and state
graders wired into a release gate — and that the whole thing runs with no API
key, no network, and no cloud account. That last property is not a convenience.
It is the claim: a system with this much machinery in it should still be
runnable end to end in mock mode, and if it is not, the coupling has gone wrong
somewhere.

## Run it

```bash
make demo-ch28
# or
python artifacts/ch28-capstone/demo.py
python artifacts/ch28-capstone/demo.py --grade
python artifacts/ch28-capstone/demo.py --grade --drift 0.20
```

The default run walks four cases. `--grade` runs the same four repeatedly
against a drifting model and reports `pass^k` with confidence intervals and a
computed GO / NO-GO.

## The four cases

| Case | What happens | What it exercises |
|---|---|---|
| `NR-T-8812` damaged item | Reads the order and the policy, refunds 3,250c, messages the customer, done | The loop, typed tools, derived idempotency keys, telemetry |
| `NR-T-8813` high value | 8,400c is over the threshold, so the run **suspends**; the approval binds the sha256 of that exact call; the resume replays and completes | Policy at the action boundary, the approval inbox, fingerprint binding |
| `NR-T-8814` fraud handoff | Escalates to the specialist; the run was admitted **without** `refunds:write`, so a refund is unauthorised rather than merely gated | Identity, least privilege, policy failing closed |
| `NR-T-8815` crash recovery | The worker dies the instant the refund lands; the journal replays to the crash without re-executing anything; one refund | Durable execution, replay, the derived key |

Case four also runs the same ticket against a refund service that times out
*after* committing, with and without a derived key:

```
    derived key : 1 refund(s), 3250c against a 3250c claim, status=succeeded
    no key      : 2 refund(s), 6500c against a 3250c claim, status=succeeded
```

Both report success. Only the ledger tells them apart. That is Chapter 1's
incident, and it is what the four mechanisms exist to prevent.

## The report

```
============================================================================
             NORTHSTAR RETURNS SUPPORT AGENT - GO-LIVE EVIDENCE
============================================================================
runs: 48   drift per turn: 10%   graded against authoritative state
----------------------------------------------------------------------------
case               n  ok  verified          95% CI   pass^2   pass^4
----------------------------------------------------------------------------
damaged_item      12  11      0.92    [0.65, 0.99]    0.833    0.667
high_value        12  12      1.00    [0.76, 1.00]    1.000    1.000
fraud_handoff     12  12      1.00    [0.76, 1.00]    1.000    1.000
crash_recovery    12  12      1.00    [0.76, 1.00]    1.000    1.000
----------------------------------------------------------------------------
what the world says, across every run:
  mutations attempted        59
  action integrity           1.000
  unauthorised side effects  0
  trace completeness         1.000
  recovery drilled           True (12 run(s) killed and resumed)
  cost per verified success  3.13c (ILLUSTRATIVE prices)
----------------------------------------------------------------------------
DECISION: GO
============================================================================
```

Every figure is computed from runs that happened.

- **Verified success** is a state grader's verdict on the authoritative world,
  not the run's own status. A run that reports `succeeded` while the ledger
  disagrees counts as a failure here.
- **pass^k** is the probability that `k` runs *all* succeed, from the observed
  pass/fail vector via `northstar_evals.pass_k`. Not pass@k, which rises
  towards 1.0 as you allow more attempts.
- **The intervals** are Wilson intervals over the same counts. A proportion
  measured over a dozen runs has an honest uncertainty of roughly twenty
  points, and reporting it bare is how a reliability review reaches the wrong
  conclusion politely.
- **Action integrity** counts mutations, not requests. A duplicate refund or a
  duplicate customer message fails it even when the run succeeded.
- **Recovery drilled** is the test for the second rung of the maturity ladder.
  Not "durability exists in the code" — a worker was killed mid-mutation and
  the run came back without double-paying, twelve times.
- **Prices are illustrative placeholders.** The mock model is free, which is
  true and useless for a cost report, so the capstone prices it at a
  placeholder rate and says so everywhere the number appears.

The decision follows from the targets in `gate.TARGETS`; it is not asserted.
Raise `--drift` and it flips, and the demo proves that by running the same
suite at four times the drift and showing it blocked.

## Files

| File | What it is |
|---|---|
| `admission.py` | The admission layer: three identities, risk classified from authoritative state rather than from the ticket text, budgets and scopes per tier, the configuration hash pinned to the run, capacity rejection, and the durable run record. |
| `capstone.py` | The system, assembled per case: `DurableRunner` over `AgentLoop`, the policy decision point with a scope rule on every write, the approval store, the tool registry with derived keys, and `Instrumentation` with a `CostLedger`. `CaseResult` is the evidence bundle. |
| `scenarios.py` | The four cases: ticket, script, and the graders that settle each one. |
| `gate.py` | The go-live report: `pass^k` with Wilson intervals, action integrity, unauthorised side effects, trace completeness, recovery drilled, and the computed decision. |
| `demo.py` | Runs the four cases, then `--grade` for the report. |
| `test_ch28.py` | The same properties as assertions. |

## Two places the code makes a decision worth arguing with

**Risk tier and approval threshold are different controls.** The damaged-item
ticket and the high-value ticket are the same order, so admission classifies
both at the `high_value` tier and gives both the same budget and scopes. The
difference appears at the action boundary: 3,250 cents is under the threshold
and 8,400 is not. Admission bounds what a run *may* do; policy decides each
call. Collapsing the two loses the ability to say why a particular call
stopped.

**`send_message` is keyed by content, `issue_refund` by step.** The runtime's
default derivation is `(run_id, step, call_id)`, which is right for a refund:
two refunds at two steps are two intents, and the second is a decision the
agent made rather than a retry of the first. It is wrong for an apology — the
same message twice in one run is never an intent, and unlike a refund it cannot
be clawed back. So `capstone._message_tool` derives that tool's key from
`(run_id, content)` instead, and a model that repeats itself four turns later
still sends one message.

Which derivation a tool wants is a property of the tool contract, which is why
it is decided in the tool and not in the loop. Delete that override and run
`--grade --n 24`: the report's action integrity drops below 1.000 and the
decision becomes NO-GO, naming duplicate customer messages. That is the gate
doing its job, and it is a good way to see what the gate is for.
