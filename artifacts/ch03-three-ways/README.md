# Chapter 3 — one triage agent, three ways

**What this artifact proves:** the framework you pick changes how much glue
you write, how finely a run is checkpointed, whether a policy check of your
own gets a seat between tool selection and tool execution, and what leaves
your process by default — and it changes none of the correctness guarantee,
because that lives in the tool contract and in the refund service, exactly
where Chapter 1 left it.

## Run it

```bash
make demo-ch03
# or
python artifacts/ch03-three-ways/demo.py
```

The demo builds the same agent three times — a hand-written loop, a graph
runtime, and a hosted harness — behind one port interface, drives all three
with the same scripted model against the same world, and prints the
scorecard. Then it does the same run three more ways per port: a plain run,
a replay under the same run id, and a kill with the refund committed and
unrecorded followed by a resume on a fresh object.

It exits non-zero if any port produces a different tool-call trajectory, or
if any of those three ledgers holds anything other than one refund of 3,250
cents.

The scenario is fixed: order `NR-2026-0041903`, a travel mug, reason
`damaged`, 3,250 cents, which is below the 5,000-cent approval threshold and
therefore inside the agent's autonomy budget.

## Files

| File | What it is |
|---|---|
| `shared/triage.py` | The portable core. `REFUND` is the tool contract as data, so the same `ToolSpec` compiles into all three runtimes; written as a decorator it would have to exist three times. Also the scripted trajectory, the world binding, `refund_key`, and `CrashingRegistry` — the kill, expressed so all three runtimes can be subjected to it identically. |
| `ports/base.py` | `TriagePort`: `build`, `run`, `resume`. One signature, so the scorecard drives three runtimes without knowing which is which. |
| `ports/raw.py` | The Chapter 2 loop behind the port. The control group: every seam is a line in this repository. |
| `ports/graph.py` | An explicit node-and-edge runtime with a declared state schema. Roughly three times the glue, and it buys a checkpoint boundary at every node instead of every turn. |
| `ports/harness.py` | A hosted harness. `HostedAgent` is not a mock of a product; it is the decisions the managed runtimes have in common — a permission list instead of a policy hook, a session store instead of an execution checkpoint, and payload capture on by default. |
| `scorecard.py` | The measurements. Glue is counted from source, checkpoints from the runtime's own counter, the resume point from a real kill, the policy hook by counting evaluations, and egress in bytes of tool arguments that reached an off-process sink. |
| `demo.py` | Runs all three, prints the scorecard and the three ledgers, and asserts they agree. |
| `test_equivalence.py` | The same properties as assertions, plus the control: swap the derived key for a per-attempt key and the ledger holds two rows in every runtime. |
| `conftest.py` | Makes `import shared.triage` mean *this* chapter's when the whole `artifacts/` tree runs under one pytest. |

## Read `shared/triage.py` first

Everything in it is something a framework would otherwise make you rewrite
per runtime, and the reason it can live outside all three is that none of it
is expressed in a framework's vocabulary. The tool contract is a
`ToolSpec` — data — rather than a decorated function. The idempotency key is
a pure function of `(run_id, step)` rather than something the runtime hands
you. The trajectory is a script rather than a graph.

That is the exit inventory from the chapter, inverted: what you keep outside
the framework is what you would not have to rewrite to leave it.

## What the scorecard does not score

Two of the seven criteria have no honest number: how much control you need,
and what leaving would cost you. `print_scorecard` ends by printing them as
questions. A column of invented scores for those is the feature grid this
chapter argues against, and it would be the most confidently wrong part of
the table.

## Two places the code deviates from the chapter's excerpts

`RawLoopPort.run` passes `run_id=` through to the loop. The printed excerpt
elides it, and it is load-bearing: without it the loop generates a run id,
the derived key becomes a nonce, and the replay assertion in
`test_equivalence.py` pays twice.

`test_equivalence.py` reads the ledger through `refund_amounts(world, ...)`.
The printed excerpt writes `world.ledger.refunds(...)`, which is not an API
`northstar_contracts.World` has; `World.refunds_for(order_id)` is.
