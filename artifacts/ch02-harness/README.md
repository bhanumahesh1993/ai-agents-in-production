# Chapter 2 — a checkpointed loop, a budget, and a kill

**What this artifact proves:** a run interrupted at its most dangerous moment —
after a refund has committed and before anything has recorded that it did —
resumes on a *different process* to a correct world state, and the property
comes from where the two journal writes sit plus an idempotency key derived
from the run and the step, not from the storage backend. Move the checkpoint
between the model's decision and the dispatch and the same fault pays twice.

## Run it

```bash
make demo-ch02
# or
python artifacts/ch02-harness/demo.py
```

The demo runs three things:

1. **Correct placement.** Intent journaled before the dispatch, evidence after,
   key derived from `(run_id, step_id)`. The refund commits and the worker is
   killed on the spot. A second Python interpreter, which never saw the first
   one's memory, resumes from the SQLite file and finishes. Ledger: one refund.
2. **Wrong placement.** `checkpoint_wrong.py` saves between the decision and
   the dispatch and stamps no key. Same fault, same kill, same file. Ledger:
   two refunds, 6,500 cents against a 3,250-cent claim, run status
   `succeeded`.
3. **The worksheet.** All eight autonomy axes checked against the live
   component that holds each one, and the six-condition suitability gate run
   over damaged-item triage and over an address change.

It exits non-zero if the resumed run produces a second refund, if the wrong
boundary fails to produce one, if the second worker does not exit cleanly, or
if any axis in `autonomy_budget.yaml` has no enforcement point.

## Files

| File | What it is |
|---|---|
| `loop.py` | One turn with all ten positions visible. `step()` is the chapter's excerpt: budget checked before the model call, intent journaled before the dispatch, evidence after. |
| `registry.py` | Dispatch longhand — resolve, validate, ask policy, execute, normalise, budget the result — and it never raises. `validate`, `normalize`, `is_retryable`, and `truncate` are the four helpers it leans on. |
| `budget.py` | The three exits that belong to code, plus the no-progress detector. Measures *active* seconds, so a run suspended for an approval does not burn its deadline. Every limit raises. |
| `checkpoint.py` | `MemoryCheckpointer` (a dict, honest about it) and `SqliteCheckpointer`, whose upsert carries `WHERE excluded.step >= checkpoints.step`. That clause is the part worth copying. |
| `checkpoint_wrong.py` | The harness from the chapter's opening: checkpoint between decision and outcome, no derived key. Two defects that compound. |
| `journal.py` | The append-only record. Two writes per consequential call, `pending_tool_call` for the resume, and `replay_decisions` for rebuilding the turn a dying worker left half recorded. |
| `runner.py` | `resume(run_id)`: `UnknownRun`, `ConfigDrift`, rebuild, re-dispatch the one ambiguous call with the rederived key. |
| `refund_ledger.py` | The refund service, holding its idempotency receipts in the same SQLite file. A key only means something if the *target* honours it, and the target has to still be there when the second worker asks. |
| `autonomy_budget.yaml` | The eight axes as configuration, with Northstar's numbers. |
| `autonomy.py` | Loads that file (no YAML dependency), turns it into a `BudgetGuard` and an `AutonomyPolicy`, and reports any axis no live component reads. |
| `suitability.py` | Chapter 1's six-condition gate, failing closed. An unanswered condition is not an approval. |
| `demo.py` | Runs all three, prints both ledgers, and asserts the difference. |
| `test_ch02.py` | The same properties as assertions, on the ledger and on what the guard raises. |
| `conftest.py` | Makes `import loop` mean *this* chapter's loop when the whole `artifacts/` tree runs under one pytest. |

## Read `loop.py` first, then `journal.py`

`step()` is ten lines of ordinary code and two of them decide whether the run
is resumable:

```python
self.journal.append("tool.called", call)   # intent
result = self.tools.dispatch(call)
...
self.journal.append("tool.result", result) # evidence
```

A worker that dies between those two writes comes back knowing a call was
attempted and not knowing whether it landed. That is the *only* state from
which you can do something sensible, and it is why `journal.py` is where the
resume logic reads from rather than the checkpoint. The checkpoint is derived,
compacted, and replaced in place; it answers "where is this run now". The
journal is append-only and answers "what did this run do". If budget forces
you to keep one, keep the journal.

## Two places the code deviates from the obvious

`MutableRunState` in `loop.py` exists because `RunState` is frozen — a run's
history is evidence, and evidence you can edit is not evidence — while the
harness needs somewhere to accumulate the turn it is in the middle of.
Everything outside the loop still sees a `RunState`.

`refund_ledger.py` exists because `World` lives in memory. Chapter 1 could
demonstrate an idempotency key with both attempts inside one process. This
chapter kills the process, so the receipt has to outlive it, which means it
belongs in the service rather than in the agent. That is not an artefact of
the example: it is the reason the guarantee is available at all.
