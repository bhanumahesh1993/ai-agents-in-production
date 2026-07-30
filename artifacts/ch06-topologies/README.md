# Chapter 6 — the same task as a supervisor and as a swarm

**What this artifact proves:** topology choice trades turn count against
tokens per turn in ways that are measurable on a fixed script, and the safety
difference between two multi-agent designs lies in the contents of the
handoff rather than in the arrangement of the agents. Two swarms with the
same agents, the same tools, and the same injected fault settle the claim
once or pay it twice depending on one thing: whether the transfer carried
`origin_run_id` and `origin_step_id`. Both report `succeeded`.

## Run it

```bash
make demo-ch06
# or
python artifacts/ch06-topologies/demo.py
python artifacts/ch06-topologies/demo.py --assert-single-refund
```

Three configurations against the same fixture, with
`World.inject_fault("issue_refund", kind="timeout")` armed in all of them:

1. **Supervisor.** Two read-only workers reached through `delegate_to_worker`.
   Every write stays in the supervisor's loop.
2. **Swarm, contract carried.** Control transfers to the fraud agent, which
   derives its idempotency key with `refund_key(handoff)`.
3. **Swarm, contract dropped.** The same swarm, keyed with
   `refund_key_local(state)`.

The demo prints per-run turn counts, per-component turn counts, token totals
and tokens per turn, each handoff payload field by field, the keys presented
to the refund service, and which component was holding the write when the
timeout came back.

The default exit code is `0` when all three behave as designed — including
the deliberate duplicate in the third — so `make demos` stays a smoke test.
It exits non-zero if any configuration reports a status other than
`succeeded`, if no component can be named as the owner at the timeout, if the
over-threshold refund did not ask a human, if the carried contract presents
more than one key, if the dropped contract presents only one, or if
`handoff.yaml` and the `Handoff` dataclass have drifted apart.

`--assert-single-refund` asserts the ledger holds exactly one refund across
every configuration, which the third fails on purpose. That is the behaviour
the chapter's "Try it" box describes.

## The refund is 12,000 cents, not 24,000

Order `NR-2026-0042110` is US$240.00: two Field Speakers, one of which
arrived damaged. The claim is one unit.

That matters. The third configuration has to be able to leave *two* refund
rows, and on an order whose total equalled the claim the world's own
over-refund guard would reject the second row — so the guard, rather than
the dropped provenance, would be what stopped the duplicate, and the chapter
would be measuring the wrong thing. The same reasoning is why Chapter 1
refunds a line item rather than an order.

## Files

| File | What it is |
|---|---|
| `handoff.yaml` | The contract as a config file, with the six things that must move with control, and the one thing that must never: a raw credential. |
| `handoff.py` | `Handoff`, `refund_key`, and `refund_key_local`. Also `narrow()`, where budgets are remainders and permissions can only shrink, and `load_contract()`, so the printed file and the typed one cannot drift apart unnoticed. |
| `topology.py` | The shared fixture, `KeyedRegistry` (which stamps the write with whichever derivation the topology uses), and `Trace`, which feeds every component's `model.called` events into one `northstar_telemetry.CostLedger`. |
| `supervisor.py` | `DELEGATE`, the read-only workers behind it, and the supervisor that holds `issue_refund`. |
| `swarm.py` | `TRANSFER`, the contract it builds, and the fraud agent that receives it. The `carry_contract` flag selects the derivation. |
| `compare.py` | Runs all three and produces the table. Every number comes out of the run. |
| `demo.py` | The tables, the payloads, the keys, and the assertions. |
| `test_ch06.py` | The same properties as assertions, on the ledger and on the contract. |
| `conftest.py` | Makes `import topology` mean *this* chapter's when the whole `artifacts/` tree runs under one pytest. |

## Read `handoff.py` from the bottom

```python
def refund_key(h: Handoff) -> str:
    return idempotency_key(h.origin_run_id, h.origin_step_id)

def refund_key_local(state: RunState) -> str:
    return idempotency_key(state.run_id, state.step)
```

Chapter 1's incident had a single cause inside one agent: a mutating call
with no idempotency key, retried after a timeout that carried no information
about whether the write had landed. Those two functions are the same failure
distributed across a boundary. The receiver has a fresh run id, so the second
one gives a retried step a new identity, and one intent becomes two refunds.

The single-agent version of that failure takes two arguments to fix. The
multi-agent version takes a contract, because the failure now lives between
the components rather than inside one.

## Where this artifact and the chapter's printed table disagree

The chapter's table reports 21 turns for the supervisor, 13 and 15 for the
two swarms, and the swarm costing 1.4x the supervisor's tokens. This fixture
produces smaller turn counts and, on the *total*, a cheaper swarm.

Two of the three claims hold as printed and are asserted in `test_ch06.py`:
the swarm finishes in fewer turns, and each of its turns costs more than a
supervisor turn, because the accumulated transcript travels with the
transfer while a worker gets a scoped brief. What does not hold here is the
total: this conversation is short by the time control moves, so the
supervisor's three separate loops — each paying for its own system prompt
and tool definitions on every turn — outweigh the transcript the swarm
carries. `compare.py` prints tokens and tokens-per-turn side by side for
exactly that reason.

The chapter's owner column reads "Nobody" for the third row. The trace names
a component — `fraud-review`, at the step where the timeout was observed —
and reports `no origin anchor` beside it. Something was always executing;
what was missing was anything tying that execution back to the intent, which
is the more precise version of the same point.
