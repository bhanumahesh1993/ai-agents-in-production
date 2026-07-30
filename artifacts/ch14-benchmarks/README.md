# Chapter 14 — a benchmark adapter for the Northstar task set

**What this artifact proves:** the number a leaderboard would publish and the
number that should gate a release come from the same runs on the same tasks,
they differ by a large amount, and the difference is attributable to a named
slice of the set rather than to a general suspicion about benchmarks. On the
shipped forty-task set the headline `pass@1` is 0.95 and `pass^5` is 0.75, and
essentially all of that gap sits in the eleven dual-control tasks — the ones
where the world does not change unless a person does something.

## Run it

```bash
python artifacts/ch14-benchmarks/demo.py
python artifacts/ch14-benchmarks/demo.py --holdout
python artifacts/ch14-benchmarks/demo.py --contamination-check
```

The default run executes all forty tasks five times each against the seeded
flaky model and prints the per-task table, the two headline numbers, the p95
cost and latency from the same runs, the solo-versus-dual-control split, and a
separate count of forbidden actions taken. `--holdout` runs only the frozen
split. `--contamination-check` reports any task that shares a four-word window
with a local corpus of public benchmark phrasings.

The demo exits non-zero if `pass^5` on the money-moving tasks falls below the
configured floor, if any attempt took a forbidden action, if `pass^5` is not
below the single-attempt headline, or if the dual-control slice is not harder
than the solo one.

## Files

| File | What it is |
|---|---|
| `northstar_tasks.json` | The forty-task set in a public-benchmark-shaped schema: fixtures, user script, user actions, success predicate over authoritative state, forbidden tools, budgets, split, and provenance for every task. |
| `task.py` | `BenchmarkTask` and the loader, which refuses records that are internally contradictory. Plus the `holdout`, `train`, `dual_control` and `solo` slices. |
| `adapter.py` | `attempt`: one run, fresh fixtures, simulated user wired into the registry, dual-control gate, graded against the world. This is the whole adapter. |
| `report.py` | `run_repeated`, `compare`, the `pass@1`-beside-`pass^k` summary, cost and latency percentiles, the solo/dual breakdown, and the separate unsafe-success count. |
| `contamination.py` | Shingle-overlap check against a local corpus of public benchmark text. |
| `demo.py` | Runs the set, prints both numbers, and asserts the release floor. |
| `test_ch14.py` | The same properties as assertions, including a run that claims a refund it never made. |
| `conftest.py` | Makes `import task`, `import adapter` and `import demo` mean *this* chapter's when the whole `artifacts/` tree runs under one pytest. |

## Read `adapter.py` first

`attempt` is thirty lines and three of its decisions carry the chapter.

**`world_from_fixtures(task.initial_orders)` runs per attempt, not per task.**
Every attempt starts from the same declared fixtures and nothing else, so the
mutating `issue_refund` call cannot leak into the next attempt and a task
cannot pass by touching an order it was never given. The registry stamps a
derived idempotency key, so a retry *inside* an attempt returns the first
receipt instead of paying twice.

**The graders never read `run.final_text`.** `expected_refund_cents` is
asserted against the refund ledger. `test_ch14.py` runs a model whose only
output is "I have issued the refund of 3250 cents. All done." and the attempt
fails with an empty ledger, which is the whole argument for state-based
predicates in one test.

**Forbidden tools are graded separately from the outcome.** A task that
reached the right final state through an action nobody would have approved is
a failure, and it is counted in its own series rather than folded into the
pass rate. The escalation-only cases forbid `issue_refund` outright, which is
what makes "no money moved" a rule the run had to follow rather than an
accident it happened to have.

## How dual control is modelled

Eleven of the forty tasks declare `user_actions`: a photo of the damage, a
confirmed delivery address, the last four digits of the card. Two mechanisms
implement them, and together they reproduce the τ²-bench finding that guiding
a person is harder than acting alone.

The customer only acts if the agent asks in terms they recognise. `ACTION_CUES`
maps each action to the phrase the outbound message has to contain. A run that
never asks gets a refusal from the write tools and cannot finish, which
`test_ch14.py` asserts by handing the agent the same plan with the request step
removed.

And a customer who was asked correctly complies only `COMPLIANCE` of the time,
drawn from a generator seeded by the attempt and the task. That constant is a
declared parameter of the simulator, not a measurement; it is set near the
magnitude the published dual-control drop suggests, so the gap the report
prints is the right order of size. Everything downstream of it — every rate,
percentile and breakdown — is computed from the runs.

## Three places the code differs from the chapter's excerpts

**`World.from_fixtures(...)` is `world_from_fixtures(...)`.** `World` belongs
to `northstar_contracts`, and an artifact does not get to grow the contracts
package a classmethod that one chapter needs. The behaviour is the printed
behaviour: a fresh world holding exactly the declared fixtures.

**`ToolRegistry.northstar(world, user)` is `northstar_registry(world, user,
control)`.** Same reason, plus the dual-control gate has to be wired in
somewhere and the registry is where the tools are.

**`attempt` returns an `AttemptResult`, which *is* a `GradeResult`.** The
chapter's `attempt` returns a `GradeResult` and its `compare` reads
`a.cost_cents` off an attempt. Both are true here: `AttemptResult` subclasses
`GradeResult` and adds the cost, turn count and latency of the same run,
because a bare `GradeResult` has nowhere to put them and a release decision
needs them from the run it graded.

## What the contamination check is not

It is a shingle-overlap test against a small local corpus. It catches
copy-paste and nothing else. A clean report is not evidence that your set is
private in the sense that matters: if you evaluate through an endpoint whose
terms permit training on inputs, the set is one API call from becoming public
evaluation data, and you will keep trusting the number it produces.
