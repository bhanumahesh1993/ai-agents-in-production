# Chapter 13 — a repeated-run harness with intervals

**What this artifact proves:** a reliability claim about an agent is a claim
about a distribution, so a single green run supports nothing, a suite mean
built from a slice that works and a slice that does not describes neither, and
the number that decides a launch is `pass^k` with an interval on it — measured
per duration bucket, against authoritative state, on fixtures rebuilt for
every repetition. Every figure the demo prints is computed from the runs it
just executed; none of them is asserted.

## Run it

```bash
python artifacts/ch13-reliability/demo.py
python artifacts/ch13-reliability/demo.py --compare
```

The default run does five things:

1. **Measures the twelve-task critical set**, twenty runs each, and prints
   `pass@1`, `pass^2`, `pass^4` and a Wilson interval per task, with a
   bootstrap-over-tasks interval on the suite line.
2. **Buckets the same runs by duration.** The short bucket needs six turns or
   fewer; the long bucket needs twelve or more. The buckets disagree, and the
   suite average describes neither.
3. **Converts each rate into an error budget** against a weekly ticket volume,
   which is what turns a measurement into a launch scope.
4. **Contrasts `pass@4` with `pass^4`** on the same twenty runs, so the size of
   the gap is visible rather than argued.
5. **Re-runs the whole suite with one world shared across repetitions**, which
   makes a correct agent score 5% on most tasks.

`--compare` runs two agent versions over the same tasks at the same seeds,
pairs the runs, and reports McNemar's exact test on the discordant pairs beside
a bootstrap interval on the effect size.

The demo exits non-zero if the suite is degenerate, if `pass^4` is not below
`pass@1`, if the long bucket is not less reliable than the short one, if the
shared-world harness does not score lower than the correct one, or if the error
budget arithmetic does not pick the scope it should.

## Files

| File | What it is |
|---|---|
| `tasks.py` | The twelve-task Northstar critical set as data: fixtures, duration bucket, plan, tool scope, injected faults, and a `StateGrader` expectation per task. |
| `metrics.py` | `pass_k` and `wilson` under the names the chapter prints, plus the bootstrap over tasks and the exact McNemar test. All four delegate to or check against `northstar_evals`. |
| `harness.py` | `run_repeated`, the suite runner, the two agent versions, and the shared-world harness kept deliberately broken. |
| `error_budget.py` | Objective plus volume in, expected failures and a verdict out. The arithmetic that turns a rate into a launch decision. |
| `compare.py` | `compare_versions`: two arms, same tasks, same seeds, paired, with a p value and an effect size. |
| `demo.py` | Runs all of it and asserts the properties. |
| `test_ch13.py` | The same properties as assertions, plus the estimators checked against their closed forms. |
| `conftest.py` | Makes `import demo` mean *this* chapter's demo when the whole `artifacts/` tree runs under one pytest. |

## Read `tasks.py` first, then the one line in `harness.py`

The line is `world = task.build_world()` inside `grade_once`. Hoist it out and
run 2 starts with run 1's refund already in the ledger, so a grader asserting
"exactly one refund of 3,250 cents" fails every run after the first. The
harness then reports 5% for an agent that is behaving correctly, and 5% looks
exactly like a regression. `run_shared_world_suite` ships that bug on purpose
so the demo can print both tables side by side, and `test_ch13.py` asserts the
broken one scores at most one success out of ten.

`tasks.py` is worth reading because of what a task's `plan` is. A positional
script loses a step every time the flaky wrapper wastes a turn, which makes
*any* interference fatal and makes every task decay at the same rate — a
measurement of the script, not of the agent. A `Plan` re-derives its next
action from the calls that have actually succeeded, in order, which is what a
competent agent does. A wasted turn then costs a turn and not a step, and a
run fails when it runs out of turns, repeats a write, or stops early.

That is the whole mechanism behind the bucketed table. Every task gets the
same `TURN_SLACK` of spare turns beyond the length of its own plan. A long
plan has more draws against that same fixed tolerance, so it exhausts it more
often. The harness is never told which bucket a task is in.

## Two places the numbers deserve a caveat

**The flakiness rates are inputs, not findings.** `BASELINE` declares how
often the model repeats itself, stalls, or stops early. Those three numbers
were chosen so the suite lands in the region the chapter is about — high
`pass@1`, visibly lower `pass^k` — rather than at either extreme where the
arithmetic stops being interesting. Everything downstream of them, including
every rate, interval, p value and budget line the demo prints, is computed
from the runs.

**A repeat of `issue_refund` costs money here, and that is deliberate.** The
harness stamps a derived idempotency key on every write, so the runtime's own
retry after a timeout collapses to one refund — that is what makes
`refund-after-timeout` a recovery measurement instead of a guaranteed failure.
A repeat the *model* chooses lands in a new step, derives a new key, and pays
twice. The distinction between a retry the harness controls and a retry the
model invents is the whole of Chapter 1 restated as a measurable rate.
