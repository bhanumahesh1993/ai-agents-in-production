# Chapter 7 — compaction you can measure, and the fact it must not drop

**What this artifact proves:** a compaction strategy that looks fine on token
savings alone can be producing incorrect outcomes at a measurable rate, and the
fix is not a better summariser but a block of facts computed from the event log
that never reaches the summariser at all. The same twelve-task set, the same
summariser, and the same token ceiling separate a configuration that pays every
customer once from one that pays eight of them twice.

## Run it

```bash
make demo-ch07
# or
python artifacts/ch07-context/demo.py
```

The demo puts three configurations side by side on the same twelve tasks:

1. **No compaction.** The window grows, every token is re-sent on every
   subsequent turn, and the longest sessions exhaust the run's cost ceiling
   before they finish. Peak context passes the budget's own total.
2. **Naive compaction.** A good summariser, a correctly aligned boundary, and a
   step-span pointer back into detail. It holds the token ceiling. It also
   reproduces the opening incident's duplicate refund on eight of the twelve
   tasks, at 6,500 cents against a 3,250-cent claim.
3. **Pinned compaction.** The same compactor with `pinned_facts` prepended.
   Same ceiling, same cost per turn, same summary text, duplicate rate zero.

It exits non-zero if the pinned configuration ever produces a duplicate refund,
if it fails any task, if the naive configuration produces none (which would mean
the task set is too short to exercise the failure), if nothing ever compacted,
or if no uncompacted run exhausted its budget.

## Files

| File | What it is |
|---|---|
| `budget.py` | `ContextBudget` with per-category caps, and the accounting that charges each message to a line item. `exceeded` returns names rather than a boolean, which is the whole design opinion. |
| `pinned.py` | The facts that survive compaction, computed from the event log by ordinary code. `WRITE_TOOLS` is derived from the registry's `writes` flag, so a new mutating tool is pinned the day it is registered. |
| `compact.py` | `compact` and `naive_compact`, identical but for one statement, plus `align_boundary`, `summarize`, and the deterministic `Summarizer`. |
| `history.py` | `get_run_history(from_step, to_step)`, the retrieval path that turns compaction into paging instead of amnesia. |
| `session.py` | The scripted Northstar session, `ScriptedSession`, and `remembers_refund` — the predicate that asks what the model can actually see. |
| `eval_compaction.py` | `measure`, `run_one`, `compare`, and the `CompactingModel` middleware. The twelve tasks differ only in how long the customer wanders before asking about the refund again. |
| `demo.py` | Runs all three configurations and asserts the difference. |
| `test_ch07.py` | The same properties as assertions, on the ledger and on the assembled message list. |
| `conftest.py` | Makes `import budget` mean *this* chapter's budget when the whole `artifacts/` tree runs under one pytest. Chapters 2, 7, and 11 all ship one. |

## Read `session.py` first, then `compact.py`

`remembers_refund` is the experiment. It asks what the model can see, and it
accepts exactly two kinds of evidence: a pinned block asserting a committed
write, or the original `issue_refund` observation still resident in the window.
A summary that says "a partial refund is on its way" does not count, and that is
not a technicality — prose about a refund is a claim about the conversation, and
a receipt id with an amount in cents is a claim about the world.

Then read the two compactors. They differ by one statement:

```python
Message(role="system", content=pinned_block(log)),   # this line
Message(role="system", content=summarize(older, model, span=...)),
*keep,
```

Everything else is identical. Both align the boundary, both carry a step span,
both are idempotent, and both produce the same summary text.

## Three deviations from the chapter, stated rather than hidden

**`ScriptedSession` instead of `FakeModel`.** `FakeModel` derives its turn index
by counting assistant messages in the conversation, which is the right design
everywhere else in this repository and the wrong one here: a compactor removes
assistant messages, so the script would silently rewind and re-run earlier turns.
That would produce a duplicate refund for a harness reason rather than a context
reason, which is exactly the kind of measurement artefact this chapter warns
about. `ScriptedSession` keeps its own turn counter and lets a step read the
visible messages before deciding.

**`World()` instead of `World.fixture()`.** The chapter's excerpt names a
constructor the contracts package does not have. `World()` already loads the
three Northstar orders.

**The middleware wraps the model, not the loop's internals.** `AgentLoop`
assembles its own message list and hands it to a provider, so the honest place
for something that rewrites that list is in front of the provider.
`with_compaction(loop, BUDGET)` installs it there and changes nothing in
`northstar_runtime`.

## The idempotency key is deployed here, and does not help

`northstar_tools` builds its registry with `inject_idempotency_key=True`, so
every refund this agent issues carries a key derived from the run and the step.
Chapter 1's repair, fully applied. Both refunds in the naive configuration carry
a key, and the keys differ, because step 12 and step 33 are different steps. The
refund service does exactly what it was asked to do. The duplicate was chosen,
not retried, and no key mechanism addresses a choice.
