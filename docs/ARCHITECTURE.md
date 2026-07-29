# Architecture of this repository

The book argues that a production agent system separates into six planes. This
repository is laid out along the same seams, so a chapter's artifact drops into
one plane without reaching across the others.

```
                 ┌─────────────────────────────────────────────┐
   experience    │ approval inbox, demo CLIs, artifact demos    │
                 ├─────────────────────────────────────────────┤
   admission     │ northstar_policy: Principal, budgets, PDP    │
                 ├─────────────────────────────────────────────┤
   run           │ northstar_runtime: AgentLoop, Checkpointer,  │
                 │                    DurableRunner, Journal    │
                 ├─────────────────────────────────────────────┤
   intelligence  │ northstar_runtime: ModelProvider            │
                 │   FakeModel · FlakyModel · LiveModel         │
                 ├─────────────────────────────────────────────┤
   action        │ northstar_runtime: ToolRegistry              │
                 │ northstar_contracts: ToolSpec, World tools   │
                 ├─────────────────────────────────────────────┤
   data          │ northstar_contracts: RunState, EventLog,     │
                 │                      World, side-effect      │
                 │                      ledger                  │
                 ├─────────────────────────────────────────────┤
   control       │ northstar_telemetry: spans, CostLedger       │
                 │ northstar_evals: graders, pass^k, replay     │
                 └─────────────────────────────────────────────┘
```

## Dependency direction

`contracts` depends on nothing. `runtime`, `policy`, `telemetry`, and `evals`
each depend on `contracts` and on nothing else in this repository. Nothing
depends on `artifacts/`.

That single rule is what keeps the examples honest. A chapter cannot
accidentally teach a design where the policy layer reaches into the loop's
internals, because the import would not resolve.

```
contracts ◄── runtime
    ▲   ▲◄── policy
    │   ◄──── telemetry
    └──────── evals
```

## Why mock mode is the default, not a convenience

An agent's behaviour is a probability distribution. If your test harness samples
from a live model, a failing test tells you almost nothing: you cannot separate
a regression from variance without many runs. `FakeModel` makes the trajectory a
function of the goal, so a test that fails means the code changed.

`FlakyModel` exists for the opposite purpose. When you *want* variance — a
reliability measurement, a recovery test — you get it from a seeded generator, so
the variance is reproducible too.

`LiveModel` is lazily imported. Importing `northstar_runtime` never imports a
provider SDK, and `pip install -e .` pulls no runtime dependencies at all. This
is enforced by CI, which runs with no credentials.

## The World is the authority, not the transcript

`northstar_contracts.world.World` holds orders, refunds, messages, and an
append-only side-effect ledger. Graders read the World. This is deliberate and
it is the repository's most important design choice: the book's opening incident
is a run that *reported* success while the World held two refunds. A test suite
that grades the agent's own account of itself cannot catch that class of bug.

`World.inject_fault(tool, kind=...)` reproduces the incident. The interesting
kind is `"timeout"`, which commits the write and *then* raises, because that is
the case no client can distinguish from a write that never landed.

## Idempotency is derived, never generated

```python
idempotency_key(run_id, step_id)  # sha256(f"{run_id}:{step_id}")[:32]
```

A random key per attempt is a nonce, not an idempotency key: the retry presents a
new identity for the same intent. Deriving the key from the run and step means
every retry of the same logical step — from the HTTP client, from the model
deciding to call again, or from a worker that restarted and replayed — presents
the same key. `issue_refund` honours it. Called without one, it double-pays, on
purpose, so Chapter 1 has something to demonstrate.

## Approvals bind a call, not a session

`approval_fingerprint()` hashes the canonical JSON of the exact tool call. An
approval record stores that fingerprint. On resume, the run recomputes the
fingerprint and compares. Change the amount by one cent and the prior approval no
longer applies — the run re-requests instead of proceeding. Without this, an
approval is a session-wide permission slip, which is how "the human approved it"
stops meaning anything.

## Durable execution is a journal plus replay

`DurableRunner` appends a record per step before the step's effect is considered
done, then rebuilds state by replaying the journal. Replay must be
deterministic, so workflow code reads neither the wall clock nor a random source;
both come in as recorded inputs. `ReplayDivergence` is raised loudly rather than
papered over, because a silent divergence is worse than a crash.

## Adding a chapter artifact

```
artifacts/chNN-slug/
  README.md      # first line states what this artifact proves
  <modules>.py   # the code the chapter excerpts, with the same names
  demo.py        # prints the trajectory and exits non-zero if the property fails
  test_*.py      # at least one test; collected by the root pytest run
```

Rules: import from `packages/`, never copy code between artifacts; no network, no
credentials, no sleeps longer than a few milliseconds; keep any line the book
prints under 80 columns.
