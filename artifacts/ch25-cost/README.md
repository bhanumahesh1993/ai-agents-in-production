# Chapter 25 — budgets, a cache, and a router that knows what it costs

**What this artifact proves:** a cost optimisation can only be judged against
graded outcomes, and the same three techniques — per-run budgets, prefix-stable
caching, and capability routing — either reduce or increase cost per verified
success depending on the task. The routed configuration here deliberately makes
the invoice cheaper and the work more expensive, and both numbers are computed
from runs that actually happened.

## Run it

```bash
make demo-ch25
# or
python artifacts/ch25-cost/demo.py
```

The demo runs the same six Northstar tickets four ways — baseline, cached,
routed, and cached-plus-routed — and prints, for each: model calls, input
tokens, cached tokens, output tokens, cents, cents per call, verified successes,
modelled p50 and p95 latency, escalations, human-handling cost, and cents per
verified success. Then it shows three properties of the machinery: a stable
prompt prefix against one with a run id at the front, a cache that will not
serve one tenant from another's entry, and a budget that raises.

A representative run:

```
config         calls    input   cached  output  cents  c/call
baseline          16    24911        0     380      9   0.503
cached            16    24911    20910     380      3   0.150
routed            17    26551        0     408      5   0.272
cached+routed     17    26551    22304     408      2   0.077

config          ok   rate   p50 ms   p95 ms  esc  human c  c/success
baseline       6/6   100%     1905     1965    0        0       1.34
cached         6/6   100%     1717     1777    0        0       0.40
routed         5/6    83%     1210     2036    1       30       6.93
cached+routed  5/6    83%     1022     1785    1       30       6.26
```

Caching is a straight win: same graded outcomes, a third of the bill. Routing
halves the cost per call and multiplies the cost per success by five.

It exits non-zero if routing fails to reproduce that inversion, if caching
changes a graded outcome or fails to reduce spend, if an unstable prefix still
gets a cache hit, if one tenant's prefix warms another's, if the deterministic
escalation check never fires, or if a one-cent budget fails to stop a run.

## Why the routed configuration loses

Two of the six tickets carry the whole argument.

`over_refund_mug` is what escalation is *for*. The cheap model asks to refund
4,900 cents against a 3,250-cent order. The amount sits deliberately under the
5,000-cent approval threshold, so policy is not what stops it — the tool is.
Nothing lands, the deterministic check fails on the schema, and the step is
redone on the large model at a cost the harness counts. The ticket succeeds.

`changed_mind_mug` is the trap. The cheap model reads the same policy the large
one reads, sees the 50% SKU override on the travel mug, and refunds at face
value anyway: 3,250 cents where the customer is owed 1,625. That result is well
formed, and it reconciles against the ledger — the refund row exists, the amount
matches, the order is not over-refunded. Schema, arithmetic, and lookup all
pass. Only the state grader, which knows what the policy said, disagrees. This
is the failure a deterministic check cannot see, and the reason the escalation
check is honest about being narrow rather than being replaced with a second
model's opinion.

## Files

| File | What it is |
|---|---|
| `budgets.py` | The three limits from the chapter's excerpt — 120 cents, 12 turns, 90 seconds — the illustrative price table, and `priced()`, the loop's cost function. |
| `cache.py` | `cache_key(tenant, prefix)`, scoped by tenant *first*; the prefix-stable assembler; and `PrefixCache`, which counts hits, misses, and cached tokens. |
| `router.py` | `route(step)` over `CHEAP_STEPS`, and `escalate()` built from `schema_ok` and `ledger_reconciles` — schema, arithmetic, and lookup, never a model. |
| `scenarios.py` | The six tickets, each with a large-model script, a small-model script, and the state grader that settles whether it worked. |
| `compare.py` | The four-way harness. `MeteredModel` routes, prices, and times every call; `run_scenario` steps the loop by hand so the check can look at a turn before the next one is taken; `ConfigReport` computes every reported statistic. |
| `demo.py` | Runs all four configurations, prints both tables, and asserts the inversion. |
| `test_ch25.py` | The same properties as assertions, on measured quantities. |

## Read `compare.py`'s `run_scenario` first

It steps the loop by hand rather than calling `resume`, and the reason is the
five lines in the middle:

```python
if model.class_of(turn) == "small":
    failed = [r for r in _results_added(state, nxt) if escalate(r, world)]
    if failed and len(world.ledger) != effects_before:
        notes.append("escalation refused, this is a compensation case")
    elif failed:
        model.force_large.add(turn)
        nxt = loop.step(state)
```

A cascade is verify-then-decide, so something has to hold the turn open long
enough to look at it. And a step that already changed the world is never redone:
re-running a committed mutation is not an escalation, it is a duplicate. Every
write in this artifact goes through a registry with `inject_idempotency_key=True`,
so a *retry* of the same call collapses; the escalated call has different
arguments, which is a different intent and a case for compensation rather than
for a key.

## Two numbers that are not measurements

**Prices are illustrative placeholders.** `large-model-1` at 300/1500 cents per
million and `small-model-1` at 30/150 are round numbers chosen to make the
arithmetic legible. The cache-read multiplier of 0.1 and the 30 cents of human
handling per failed run (180 seconds at an illustrative US$6/hour, the figure
the chapter uses) are the same. Replace all of them with your own dated table
before drawing a conclusion about real money.

**Latency is modelled, not measured.** A mock model returns in microseconds, so
wall-clock timing here would report the speed of Python. `MODEL_LATENCY_MS` and
`TOOL_LATENCY_MS` in `compare.py` are the declared table the p50 and p95 figures
are computed from — time to first token, a per-output-token generation rate, a
prompt-processing rate that is ten times cheaper for cached tokens, and 40ms for
a read against 220ms for a write. That last gap is why tool latency, not token
generation, usually dominates an agent's end-to-end time.

Everything else in the report — tokens, cached tokens, cents, escalations,
verified successes — is counted from runs that happened.
