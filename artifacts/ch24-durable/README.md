# Chapter 24 — a run continued, not run again

**What this artifact proves:** a run killed at any of four points, including
inside the window where a money-moving call had started and not finished,
resumes on a different context and leaves exactly one refund in the ledger; a
key derived from `(run_id, step_id)` is what makes that true, and replacing it
with a nonce produces the duplicate immediately; and a change to the step
sequence raises `ReplayDivergence` loudly rather than replaying a program that
never ran.

## Run it

```bash
make demo-ch24
# or
python artifacts/ch24-durable/demo.py
python artifacts/ch24-durable/demo.py --unsafe-key
python artifacts/ch24-durable/demo.py --unsafe-clock
python artifacts/ch24-durable/demo.py --replay-test
```

The default run walks the whole contract: a crash between the refund's intent
and outcome records with the journal printed line by line, then all four crash
points resumed, then the same crash through `northstar_runtime.DurableRunner`,
then a suspension across a human decision, then a stream whose connection dies
and reconnects, then both unsafe variants, then the replay corpus.

`--unsafe-key` is the one to run second. It crashes at the same point with the
key derived from a nonce instead of from journaled identity, and the ledger for
`NR-2026-0041827` ends up holding two refunds totalling 6,500 cents against a
3,250-cent claim. `--unsafe-clock` shows the *silent* version: two attempts
compute two different deadlines and two different keys, the step sequence
matches throughout, and nothing raises. `--replay-test` replays the four
shipped journals against the current build and then mutates one to prove the
divergence check still fires.

The demo exits non-zero if a resumed run leaves more than one refund row, if
`--unsafe-key` stops producing the duplicate it exists to show, if a replay
executes anything or touches the world, if the reconnect leaves a gap or a
duplicate, or if a changed step sequence fails to raise.

## Files

| File | What it is |
|---|---|
| `workflow.py` | `RunContext` — `step()`, `now()`, `key_for()`, `await_approval()` — and `refund_workflow()`, the deterministic half. `step.started` is written before the callable runs and `step.completed` after it returns, which is the whole ordering contract. |
| `crash.py` | The harness: `start()`, `resume()`, and `trace()`. `DurableRun.refund_rows` reads the world's ledger, never the run's account of itself. |
| `unsafe.py` | Both versions of the two most common replay-safety violations, side by side, and `compare()`, which shows which one drifted. |
| `stream.py` | The resumable SSE endpoint: `stream()`, `sse()`, `event_id()`, and a `StreamClient` that reconnects with `Last-Event-ID` and reports whether it saw a gap. |
| `corpus.py` | `load_corpus()`, `replay()` in strict mode, and `record()` for regenerating the shipped files — which is a deliberate, reviewed act, not a convenience. |
| `journals/*.json` | Four recorded journals: a clean refund, a crash after the refund committed, an approval then a refund, and a crash after the first read. Shipped as files so a build cannot regenerate the corpus it is being tested against. |
| `demo.py` | The whole contract, with every property asserted. |
| `test_ch24.py` | The same properties as assertions on the ledger and the journal. |
| `conftest.py` | Path handling for this directory, plus the function-scoped `world` and `journal_dir` fixtures. |

## Read `workflow.py`'s `step()` first

Three branches and the chapter is in them:

```python
if step_id in self.completed:          # replay: return, invoke nothing
    return self.completed[step_id]
self._write("step.started", ...)       # the intent, before the effect
result = fn(*args, **kwargs)
self._write("step.completed", ...)     # only now is it done
```

A step whose intent was recorded and whose outcome was not falls through the
first branch, so it is **re-issued** rather than skipped. That is resolve, do
not repeat, implemented as an ordinary retry of an idempotent step — and it is
safe only because `key_for()` recomputes the same key from the run id and the
journaled step identity. The engine guarantees it will not lose the step and
will not forget that it completed. It cannot guarantee that a payment API you
do not control treats the second call as a duplicate; that half is the key's.

## Two places this deviates from the chapter, on purpose

**Nothing exits the interpreter.** The chapter's harness kills the process for
real. A demo that killed its own interpreter could not then print what
happened, and CI could not tell a demonstration apart from a crash, so
`SimulatedCrash` unwinds the stack instead. The journal state reached is
identical, because the records written before the raise are already durable.
Chapter 8's demo does exit the process between phases, and that is where you
can watch a real one.

**`step_id` is a name, not a counter.** The chapter writes
`ctx.step_id("refund")`. Here the step's identity is derived from its name, so
inserting a read in front of a write does not silently renumber the write and
mint it a new idempotency key. A positional counter satisfies the derivation
rule and breaks the moment someone adds a step, which is the failure mode the
rule exists to prevent.

## What the four journals are for

`replay()` runs in strict mode, where a step the journal does not hold raises
rather than executing. Two outcomes are healthy: a complete journal replays to
its terminal state, and an incomplete one replays to the state at the moment
the worker went away. `04-crash-after-first-read.json` is the incomplete one,
and it reports `interrupted` rather than failing — because "how far did this
get before the worker died" is the first question in any incident, not a fault.

Only a journal holding a *different* step at a given position is a failure, and
that one raises. Add a step to `refund_workflow()` and three of the four
entries stop replaying, which is exactly the signal a version gate exists to
answer.
