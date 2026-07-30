# Chapter 20 — an approval that binds one call, in one run

**What this artifact proves:** a recorded approval binds one exact call, in one
run, for a bounded time; a modified call is rejected by the mechanism rather
than by anyone's vigilance; and the run's hard caps stop it without a human
being available at all.

## Run it

```bash
make demo-ch20
# or
python artifacts/ch20-approvals/demo.py
python artifacts/ch20-approvals/demo.py --fail-on-tamper
```

The demo runs the 24,000-cent refund on flagged order `NR-2026-0042110` to a
pause, prints the rendered approval payload, then drives three endings:

1. **Approve and resume.** The exact call is approved by `rota:fraud-review`,
   the run resumes, and the ledger holds one row of 24,000 cents.
2. **Approve, then the run re-plans.** The parked call's amount becomes 240,000
   before resume — the June incident, where the customer sent a second message
   overnight and the agent re-planned. The fingerprint changes, no decision
   matches it, the run returns to `waiting_approval` with a diff, and the ledger
   stays empty.
3. **Nobody answers.** The request escalates to the rotation at four hours, to
   the duty manager at eight, and expires to rejection at twelve. The ledger
   stays empty.

It then shows an approver *correcting* 24,000 to 8,400 — the same edit as
ending two, and it proceeds, because a decision exists for exactly that
fingerprint — a tool-version bump invalidating a parked approval, the write cap
raising, the never-permitted action refused by the schema, and the containment
ladder.

`--fail-on-tamper` makes ending two fatal on its own, which is the form the
chapter describes: an unbound fingerprint, and an empty ledger. Without it the
demo exits non-zero only when something behaves differently from the above.

## Files

| File | What it is |
|---|---|
| `fingerprint.py` | `fingerprint(call, principal, run_id, tool_version)`, and `bind()`, which is the *body* that goes under the hash. The hashing itself is `northstar_policy.approval_fingerprint`; `ToolVersions` holds the declared version and can bump it. |
| `outcomes.py` | `GuardOutcome` (proceed or wait, and nothing else) and the six named `RESUME_CHECKS`. |
| `inbox.py` | `TaskInbox`, a file-backed inbox with approve, reject, correct, and escalate; `ApprovalRecord.check()`, which runs the six resume checks; `ESCALATION_LADDER`. |
| `payload.py` | The six-part payload, `preview_refund()` (a dry run against the world, never the model's account of it), and the YAML renderer. |
| `payload.example.yaml` | The reference payload, and the file to read before any of the code. |
| `budget.py` | `BudgetGuard`: turns, cents, wall clock, writes, and distinct resources. `reserve()` stops before the write; `record_write()` counts what landed. |
| `classes.py` | The four action classes, Northstar's assignment table, the policy bundle, and `refund_to_non_payer()` — the never-permitted action, refused by the schema. |
| `guard.py` | The middleware. `Guard.guard(call, state)` is the chapter's excerpt; `Guard.evaluate()` adapts the same code to the loop's `PolicyEngine` protocol. |
| `containment.py` | The five ladder rungs as callable operations, the four rules as checkable properties, and `Tripwire`, which raises the containment level and never allows. |
| `run.py` | The whole path wired together: `start_run()` runs to the pause, `replan()` rewrites the parked amount. |
| `demo.py` | Three endings, plus correction, version bump, caps, and the ladder. |
| `test_ch20.py` | Everything around the fingerprint: the middleware's asymmetry, the caps, the payload, the ladder. |
| `tests/test_binding.py` | What the fingerprint guarantees. The shortest complete statement, and the file to read first. |
| `conftest.py` | Module isolation under one pytest run, and closing the SQLite checkpoints the suite opens. |

## Read `tests/test_binding.py` first, then `guard.py`

The whole control is four lines:

```python
approved_fp = fingerprint(approved, PRINCIPAL, RUN_ID, TOOL_VERSION)
tampered_fp = fingerprint(tampered, PRINCIPAL, RUN_ID, TOOL_VERSION)
assert inbox.find(tampered_fp) is None
assert inbox.is_approved(tampered, RUN_ID) is False
```

Ten times the amount, one changed integer, no matching decision.

`guard.py` carries the asymmetry that makes it usable. Budget exhaustion and
policy denial are **raised** — they end the run and callers do not catch them.
An approval requirement is **returned** — it is a state the run lives in, which
the loop turns into a checkpoint, a `waiting_approval` status, and an
`approval.requested` event.

## Four places the code deviates from the chapter's excerpts

**The excerpts are abbreviated; the code is not.** `guard()` in the chapter
shows five branches. The real one also fails closed on an unknown tool, on an
unknown tool version, and on a policy service that is unreachable *for a write*,
and it reserves the write budget before returning proceed rather than after the
money has moved.

**`fingerprint()` does not hash anything itself.** It builds the body and hands
it to `northstar_policy.approval_fingerprint`, which is canonical JSON plus
sha256. Two canonicalizers in one repository is one more than any system can
afford, and the second one is always the one that drifts.

**`TaskInbox` subclasses `ApprovalStore` rather than wrapping it.** The agent
loop calls `approvals.is_approved(call, run_id)` and `approvals.request(...)`,
so a subclass drops into the real runtime and the binding gets exercised by the
loop rather than by a fixture. Every property in `tests/test_binding.py` is
therefore also true of the run in `test_ch20.py`, which is the point.

**`replan()` is used twice, and the difference is the chapter.** Ending two
calls it to change 24,000 into 240,000 and the run is refused. The correction
path calls it to change 24,000 into 8,400 and the run proceeds. The same
function, the same edit shape, and the mechanism does not care who is honest —
it cares which call a human actually agreed to.

## One place the chapter's prose and the code disagree, on purpose

The action-class table reads "`issue_refund`, at or under 5,000 cents: sampled"
and "above 5,000 cents: always approved". The rule is `amount_at_or_above(5000)`,
which is `>=`, so a refund at exactly 5,000 cents needs a human. The rule is
what runs, and it is the right way round: a threshold you can sit exactly on is
a threshold someone will sit exactly on.
