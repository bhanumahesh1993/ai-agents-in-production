# Chapter 4 — five patterns, one task, printed costs

**What this artifact proves:** reasoning patterns trade tokens and sequential
round trips for capability on the happy path, and the cheapest addition on
the ladder — a read-only check against the system of record — is the only one
that detects a silent write failure. A critic reviewing the agent's own
transcript approves an incident, because everything in that transcript is
true.

## Run it

```bash
make demo-ch04
# or
python artifacts/ch04-patterns/demo.py
```

The demo runs six builds of the same task on a clean fixture and prints the
cost table, then re-runs all six with
`World.inject_fault("issue_refund", kind="timeout")` — the Chapter 1
incident — and prints which of them noticed.

It exits non-zero if any pattern other than the state check claims to have
caught the duplicate, if the state check misses it, if the injected timeout
fails to leave two refund rows, or if the clean fixture does not verify for
all six.

The task is ticket 8812: order `NR-2026-0041827`, the cracked lamp shade at
3,250 cents, reason `damaged`, below the 5,000-cent approval threshold.

## Files

| File | What it is |
|---|---|
| `task.py` | The fixture all six builds share: the ticket, the scripted trajectory, the tool registry, and `Meter` — which charges every model call any part of a pattern makes, inside the loop or outside it. |
| `react.py` | The baseline. Chapter 2's loop with nothing added. |
| `router.py` | `ROUTES` and `route()`: the model picks one label from a closed set, code owns everything after it, and an unrecognised label falls closed to the specialist branch. |
| `planner.py` | `PlanStep` and `validate()`: the pre-execution check that rejects a plan for an unknown tool, a write before any read, or more steps than the cap. `ToolSpec.writes` is what makes it checkable. |
| `critic.py` | One review pass over the outbound message, in a fresh context, against a written rubric, with a hard iteration cap. |
| `verify.py` | `verify_refund()`: read the ledger, not the transcript. No model call. Asserts the number of refund rows *and* their sum, because a single row for the wrong amount and two rows summing to the right amount are different bugs. |
| `search.py` | Best-of-three over the read-only sub-problem: three candidate plans, scored against the retrieved policy, one selected, and the single write executed outside the search. |
| `measure.py` | `PatternCost` and `measure()`. Runs each build over a fresh `World`, so no run sees another run's writes. |
| `demo.py` | Both tables, and the assertions that keep the chapter's central claim under test. |
| `test_costs.py` | Pins the call counts, and asserts the ledger properties rather than the run statuses. |
| `conftest.py` | Makes `import task` mean *this* chapter's when the whole `artifacts/` tree runs under one pytest. |

## Read `verify.py` first

It is nine lines of ordinary code with no model in it, and it is the only
thing in the chapter that catches the failure the book opens with. Both of
its assertions matter. Run `test_verify_refund_separates_two_different_bugs`
to see why: two refunds of 1,625 cents sum to exactly the 3,250 cents that
was claimed, and a check comparing only totals passes that run.

## Two measurement notes

**Latency in mock mode is not latency.** `FakeModel` returns immediately, so
wall-clock time here measures orchestration overhead and nothing else. The
table reports **sequential model calls** instead: round trips that cannot be
overlapped. Multiply by your provider's per-call time.

**Tokens are counted at the provider, not off the final `RunState`.** The
excerpt printed in the chapter reads `model_calls` from `state.step` and
sums `estimate_tokens` over `state.messages`. That is right for the plain
loop and wrong for every other row: a router's classification call, a
planner's generation call, a critic's review call, and a search's six
sampling calls all happen outside the loop and never appear in the final
state. `Meter` counts them where they happen.

## Where this artifact and the chapter's printed table disagree

The ratios here are computed from the run, so they are what this fixture
produces rather than what the chapter's table says. Three differences are
worth knowing about before you compare them.

The chapter's fixture is order `NR-2026-0041903`, a travel mug whose order
total *equals* the 3,250-cent claim. On that order the second half of the
demo cannot happen: the world's own over-refund guard rejects the duplicate
row, so the guard rather than the missing idempotency key becomes what
stopped the incident. This artifact uses `NR-2026-0041827` and the cracked
lamp shade — the same 3,250-cent claim as a line item on an 8,400-cent
order, which is the fixture Chapters 1 and 5 use for the same reason.

The chapter's token ratios for planning, critique, and search are larger
than this fixture produces. The direction of every one of them holds:
routing is cheaper than the baseline, verification is free, and planning,
critique, and search all cost more. The magnitudes depend on how long the
generated plan is and how much evidence travels with each branch, and this
fixture's are small.

`route()` reads `reply.text`. The printed excerpt reads `reply.content`,
which is not a field `ModelResponse` has.
