# Chapter 15 — a trajectory-eval pipeline with a replay gate

**What this artifact proves:** a suite grading only final state passes runs
that took forbidden paths, a suite grading exact trajectories fails runs that
were *better* than the reference, and predicates over the event log plus
assertions over authoritative state catch both without pinning the agent to
one way of working. All three verdicts are printed side by side from the same
case, and the two-tier gate that reads them exits non-zero.

## Run it

```bash
python artifacts/ch15-evals/demo.py

# refresh the recorded trajectories after a deliberate behaviour change
python artifacts/ch15-evals/replay.py --record
```

The demo runs one case three ways and prints a four-column verdict table:

| | exact match | state | trajectory | judge |
|---|---|---|---|---|
| **Run A** — checked the account for an earlier claim first | fails | passes | passes | passes |
| **Run B** — paged four orders, refunded, read policy after | fails | **passes** | fails | passes |
| **Ch 1** — the reference path with no idempotency key | **passes** | fails | fails | passes |

Read the two bold cells together. Exact matching rejects the best run in the
table and accepts the one that paid the customer twice. Then the demo runs the
six unsafe-success detectors, prints the four coverage families, and runs the
two-tier gate: the replay tier at 100%, then five repeats of every case with
the thresholds from `gate.yaml` evaluated one by one.

The demo exits non-zero if Run A matches the reference, if any predicate
rejects Run A, if the state grader rejects Run B, if the trajectory predicates
accept it, if `reads_outside_scope` does not fire on Run B, if the unkeyed
refund does not fail both levels, or if the gate does not pass.

## Files

| File | What it is |
|---|---|
| `cases.py` | Sixteen cases across the four coverage families, plus the chapter's Run A, Run B and Chapter 1 plans, the runner, and the graders wired per case. |
| `graders/state.py` | `RefundStateGrader`: reads the ledger, the refund rows and the order total, and reconciles all three. Never reads `run.messages`. |
| `graders/trajectory.py` | `RefundPathGrader`: five predicates and no reference path, plus `exact_match`, kept so the demo can show what it does. |
| `graders/judge.py` | `AccuracyJudge`: scores the closing message *against the event log*, so fabricated tool success scores zero. Gates two dimensions and no more. |
| `detectors.py` | The six unsafe-success detectors, all over the event log, all read-only, all usable online where there is no ground truth. |
| `sim/personas.py` | Five scripted customers with hidden goals, state-tagged scripts, and seeds. |
| `sim/world.py` | Named fixtures, each carrying distractors so a case stays losable. |
| `replay.py` | Record and replay: pinned model decisions, re-executed tools, and a config hash that has to still match. |
| `fixtures/2026-07/` | Sixteen recorded trajectories, dated. |
| `gate.py` | The two-tier gate and a twenty-line YAML reader, because mock mode has no runtime dependencies. |
| `gate.yaml` | The gate's structure and this suite's numbers. |
| `demo.py` | Runs all of it and asserts every property. |
| `test_ch15.py` | The same properties as assertions, plus each detector fired on its own evidence and silent on a clean log. |
| `conftest.py` | Makes `import cases`, `import gate` and `import demo` mean *this* chapter's when the whole `artifacts/` tree runs under one pytest. |

## Read `graders/trajectory.py` first

Five predicates, no reference path:

- `policy_before_money` — a partial order, not a total one. Nothing is said
  about where `search_orders` goes, because the answer is legitimately "it
  depends".
- `one_write` — at most one money-moving call.
- `keys_derived` — every refund carries `idempotency_key(run_id, call_id)`. A
  random key per attempt is a nonce: the retry presents a new identity for the
  same intent.
- `orders_read_ceiling` — the two-line assertion that catches Run B's
  cross-account paging, and the one no path-matching test could have
  expressed.
- `turn_ceiling` — bounds the space without describing it.

`exact_match` sits in the same file so the demo can run it, and so it is
obvious that it is a demonstration rather than a tool. A golden trajectory
earns its place as a diff target, a fixture source, and documentation. It is
not an oracle, and one older than the model version it was recorded against is
a source of false failures.

## What the replay tier does and does not re-execute

Model decisions are played back, because they are the nondeterministic input.
Tool results are **re-executed** and the recorded ones are compared against the
live ones. Playing results back would make the tier pass even when the tool
layer had broken underneath it, which is the one thing the tier exists to
notice.

Every fixture carries a `config_hash`. Replaying against a case whose
configuration has moved is replaying something else, and `replay()` reports it
as a divergence rather than as a pass. A fixture with no `config_hash` at all
is refused at load time — that is Chapter 15's operational rule in code: **a
run that cannot be graded is a failure, not a skip.** `test_ch15.py` asserts
the gate blocks on a case with no recording rather than quietly measuring the
fifteen that do have one.

## Three places the code differs from the chapter's excerpts

**`World.from_fixture(...)` is `sim.world.from_fixture(...)`.** `World` belongs
to `northstar_contracts`, and an artifact does not get to grow the contracts
package a constructor one chapter needs. `world.ledger_is_consistent()` is
`graders.state.ledger_is_consistent(world)` for the same reason — a grader is
allowed to know how to reconcile the store it grades.

**`sim/personas.py` defines its own `SimulatedUser`.** It subclasses the one in
`northstar_evals` and adds the three things Chapter 15 asks a useful simulator
to have and the base class does not carry: a hidden goal, a state-tagged
script, and a seed. `test_ch15.py` asserts the hidden goal never appears in the
agent's transcript, because a case whose goal leaks into the context is
measuring reading comprehension.

**`gate.yaml` carries this suite's `minimum_cases` and `random_trajectories`.**
Every other key and every threshold is the chapter's. Northstar's own gate says
250 and 25; this artifact ships sixteen cases, and a gate that demanded 250
would block on its own arithmetic rather than on the agent. The chapter makes
the point itself: the structure is the reusable part, and copying another
team's numbers is meaningless.

## Why the simulated tier is flaky and the replay tier is not

They answer different questions. Replay pins every nondeterministic input, so
it proves your code did not regress against last month's model behaviour and
says nothing about this month's. The simulated tier wraps the same scripted
plans in a seeded `FlakyModel`, so five repeats of a case do not all agree and
`pass^5` means something. The rates are small and declared in `cases.py`; the
outcome rate, the `pass^5` figure and the Wilson interval the gate prints are
computed from the runs.
