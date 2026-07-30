# Chapter 8 — a refund that waits three days, and a worker that really restarts

**What this artifact proves:** a run that paused in front of a 24,000-cent
refund, lost its process entirely, and came back under a different worker
performed each side effect exactly once — and you can point at the three
mechanisms that made it so: the checkpoint written before the pause returned,
the idempotency key recomputed rather than loaded, and the agent version
compared before any state was deserialised. Replace the derived key with a
nonce and the same run refunds 48,000 cents and sends Friday's notice twice.

The restart is not simulated in-process. The demo invokes itself once per
phase, so the Python interpreter genuinely exits between the pause, the
decision, and the resume, and everything the next phase knows it read back out
of two SQLite files.

## Run it

```bash
make demo-ch08
# or
python artifacts/ch08-long-horizon/demo.py
python artifacts/ch08-long-horizon/demo.py --broken-keys
python artifacts/ch08-long-horizon/demo.py --deploy-between
```

The default run drives four processes and then a report:

1. **start.** The run sends the customer a notice, reaches the refund, and
   policy returns `REQUIRE_APPROVAL` because the order is fraud-flagged and the
   amount is above 5,000 cents. The pause *returns*, the checkpoint is written
   before it does, and the process exits holding no lease.
2. **decide.** An operator answers the request on the `fraud-review` queue.
3. **resume.** A different worker runs the four checks in order and is killed
   between the refund's intent record and its outcome record. The money has
   moved and nothing has recorded that it did.
4. **resume again.** A third worker recomputes the key, asks the refund service
   whether that key already settled, and finishes.

Then the report prints the transition log with timestamps, the intent journal,
and the refund service's own rows. It exits non-zero if the ledger holds a
duplicate refund, if the customer got the notice twice, or if any intent is
still unresolved once the run is terminal.

`--broken-keys` changes one argument: the key becomes a nonce created at call
time. Phase 4 then presents an identity the refund service has never seen, and
the ledger for `NR-2026-0042110` ends up holding two refunds totalling 48,000
cents against a 24,000-cent claim, plus a second copy of Friday's notice. That
second message is the opening incident, reproduced exactly — the resume replays
the step in front of the pause, and whether the customer reads it twice depends
entirely on which key the replay presents.

`--deploy-between` runs the resume as agent v8 against a checkpoint written by
v7. The declared transformer fires and adds the field the Saturday release
added. That change is additive by every schema rule, and it rewrites the
pending call's canonical JSON, which invalidates the approval a human already
gave. The run returns to `waiting_approval` with a diff naming the field that
changed — the branch the opening incident did not have — an operator approves
the call as it now stands, and it finishes with exactly one refund.

## Files

| File | What it is |
|---|---|
| `states.py` | All eight phases, `TRANSITIONS` as the declared table, `check_transition()`, and `holds_compute()`. The interesting entries are the ones teams discover during an incident: lease expiry, expiry to failed, and a stale fingerprint sending a resuming run back to `waiting_approval`. |
| `envelope.py` | `Envelope` — a `RunState` plus `agent_version`, `schema_version`, `config_hash`, the pending call, the deadline, and the principal — over `SqliteCheckpointer`. `config_hash()` hashes the effective configuration, not the container tag. `parked()` is the pre-flight query a deploy needs. |
| `approvals.py` | `DurableApprovals`, a file-backed inbox addressed to a **queue**. `approval_fingerprint` is imported from `northstar_policy` unchanged; reimplementing it here would be a second chance to get it wrong. |
| `keys.py` | `derived_key()`, `generated_key()`, and `key_for()`. Both strategies ship so the second one can be watched failing. |
| `side_effects.py` | `SideEffectLedger` (yours: intent before the call, outcome after it) and `RefundService` (not yours: a provider that dedupes on the key you present and has no opinion about your order total). |
| `pause.py` | `Workflow`. `step()` is the chapter's excerpt — the pause that returns. `dispatch()` writes the intent, calls, then writes the outcome. `present()` re-presents one intent, which is what a replay does. |
| `migrate.py` | `plan()` returning `pin`, `migrate`, or `refuse`; `MIGRATIONS` as declared, tested transformers; and `v7_to_v8`, the additive change that breaks a fingerprint. |
| `resume.py` | The resume path and `RESUME_CHECKS`, its five stages in order. `replay_prior_steps()` is the step in front of the pause being re-executed, and `_resolve_open_intents()` is resolve-do-not-repeat as an independent read. |
| `wiring.py` | The two files and the workflow assembled in one place, so the demo and the tests are the same system. |
| `demo.py` | The orchestrator and the four phases, each in its own interpreter. |
| `test_ch08.py` | The same properties, asserted against the refund service rather than against the run's account of itself. |
| `conftest.py` | Path handling for this directory, and a per-test state directory. |

## Read `resume.py` first, then `keys.py`

Five stages, and every ordering in them is load-bearing:

```
version -> fingerprint -> decision -> prior_steps -> dispatch
```

**Version first**, because a checkpoint deserialised into code it was not
written for has been silently migrated by nobody, and every later check reads
the wrong shape. **Fingerprint before decision**, because "did a human approve
this" is meaningless until you know which call you are holding — and a stale
fingerprint *returns* the run to `waiting_approval` with a reason rather than
raising. **Resolution before dispatch**, because an intent with no outcome is a
question, and the answer is a read against the authoritative system:

```python
key = key_for(run_id, envelope.state.step, strategy)
prior = workflow.service.lookup(key)
if prior is not None:           # resolve
    ...
else:                           # nothing committed; issue it
    envelope = workflow.dispatch(envelope, with_key(call, key), key)
```

That `lookup` is only available to a worker that can *recompute* the key. With
a nonce the pre-crash attempt used an identity no surviving record holds, so
there is nothing to look up and the only available action is to pay again.

## Four places the code deviates from the chapter's excerpts

**`state.status = "waiting_approval"` is `state.with_status(...)`.** `RunState`
is frozen. A run's history is evidence, and evidence you can edit in place is
not evidence.

**`resume()` takes its collaborators as an argument.** The chapter's excerpt
reads `checkpointer`, `approvals`, and `tools` from module scope. A
module-level store is a store you cannot open twice, which means you cannot
write a test that runs two workers.

**`resume()` returns a `ResumeOutcome`, not a `RunState`.** Its `.state` is the
`RunState` the excerpt returns; the wrapper also carries which of the five
outcomes was reached, the version plan, the resolved keys, and the diff. Four
of those five outcomes are ordinary states a run lives in, and only
`refused_version` is an error — which is the whole argument of the section.

**A crashed worker's run is picked up through lease expiry, not through
`resuming`.** The second resume finds the run in `running`, because that is
where the killed worker left it. `running -> resuming` is not a declared
transition and it should not be: the chapter's table says lease expiry returns
the run to `queued`, and a worker then takes it. So the resume path makes those
two transitions visible in the log rather than quietly re-entering `resuming`.
A pickup after lease expiry also does *not* replay the step in front of the
pause, because that run never parked — the replay already happened on the
attempt that died.

## What the transition log is for

```text
  2.00  queued           -> running          by worker:v7      lease taken
  5.00  running          -> waiting_approval by worker:v7      approval.requested apr-...
  6.00  waiting_approval -> resuming         by worker:v8      lease taken
  9.00  resuming         -> waiting_approval by worker:v8      stale_fingerprint; re-requested apr-...
 12.00  resuming         -> running          by worker:v8      approved by rota:fraud-review
 13.00  running          -> queued           by lease-monitor  lease expired
 18.00  running          -> succeeded        by worker:v8      refund settled once
```

Every row carries a timestamp, an actor, and a reason, which is what makes a
run's history answerable three months later. The clock is a counter seeded from
the file rather than from the process, because a counter that restarts with the
worker stamps Monday's resume before Friday's pause — and a transition log you
cannot order is not a log.
