# Chapter 5 — one orchestrator, three readers, and two writers who disagree

**What this artifact proves:** context isolation is enforceable in code at
the tool-registry boundary rather than requested in a prompt; isolated
read-only workers cut what reaches the orchestrator by roughly an order of
magnitude without losing the evidence; and two independently correct writers
produce an incoherent world that no downstream merge, retry, or idempotency
key can repair — while both runs report `succeeded` and stay inside budget.

## Run it

```bash
make demo-ch05
# or
python artifacts/ch05-orchestrator/demo.py
python artifacts/ch05-orchestrator/demo.py --assert-coherent
```

Three acts:

1. **The orchestrator.** Three workers, three scoped questions, one crossing
   each, in parallel and never again. The demo prints what each worker's own
   context held, what actually crossed back, and the ratio between them.
2. **The parallel writers.** Two agents, one open brief, full tool access.
   `writer_a` refunds 3,250 cents. `writer_b` promises a replacement in
   writing and queues the dispatch. Both green. Both in budget. Both writes
   carry a derived idempotency key. The ledger holds a refund *and* a
   replacement promise against order `NR-2026-0041827`.
3. **The same brief, reconciled first.** The two candidate resolutions come
   back as findings from workers that cannot act on them. The lead reads
   both, chooses one, and writes once.

The default exit code is `0` when all three behave as designed — including
the deliberately broken middle act — so `make demos` stays a smoke test. It
exits non-zero if a reader accepts a write tool, if a finding exceeds its
400-token budget, if the research run writes anything at all, if act two
*fails* to reproduce the conflict, or if act three still conflicts.

`--assert-coherent` asserts on the world instead and exits non-zero, which
is the behaviour the chapter's "Try it" box describes.

## Files

| File | What it is |
|---|---|
| `subagent.py` | The factory. `reader_registry` refuses a write tool at registration time, `Finding` is what crosses back, and `compress` shrinks a run to a claim plus evidence references — never the message list. |
| `orchestrator.py` | `research()` fans out the three questions and reconciles in one place; `resolve_ticket()` is act three, the same open brief with the ambiguity settled before any write. The lead is the only principal here holding `refunds:write`. |
| `parallel_writers.py` | The whiteboard architecture from the chapter's opening, plus `conflicts()`, which reads the ledger rather than either run's account of itself. |
| `demo.py` | All three acts, the compression ratio, and the assertions. |
| `test_ch05.py` | State-based. Nothing in it asserts on a run status, because a status could not tell these two worlds apart. |
| `conftest.py` | Makes `import subagent` mean *this* chapter's when the whole `artifacts/` tree runs under one pytest. |

## Read `subagent.py` first

Three lines carry the design, and they are all about direction.

`reader_registry` raises `WriteToolInReader` when a write tool is offered.
The chapter prints this as `assert not spec.writes`; the shipped version
raises, because `python -O` erases assertions and this is a permission
boundary rather than a debugging aid.

`budget_cents` is per worker, so a fan-out of three cannot become nine.

`compress` returns a `Finding`. Outbound, the boundary carries a question, a
budget, and an identity. Inbound it carries a claim and `artifact://`
references. The orchestrator's window is the scarce resource in the whole
system, and a subagent that returns everything it saw has cost two model
calls and bought nothing.

## Why act two is not Chapter 1 again

They look similar in a ledger and they are entirely different defects.

Chapter 1 is *one* intent executed twice: a refund, retried after a timeout
that carried no information about whether the write had landed. A derived
idempotency key fixes it, and `all_tools()` here has key injection switched
on, so both writers get keys.

Act two is *two* intents, each executed exactly once, that should never both
have happened. Assert on it and you find one refund of 3,250 cents — nothing
was paid twice — alongside a written promise of a free replacement. There is
no key for that, because a key is a statement about identity of intent and
these two intents are genuinely different. The failure was in the brief,
which left "refund or replace" open, and in the architecture, which let two
components settle that open decision independently and then act on their
settlements.

## Where this artifact and the chapter's excerpts differ

`spawn_reader` takes the `World` it reads from. The printed excerpt closes
over a module-level one, which would make two concurrent research runs share
a fixture.

`ledger_for(world, name)` is a module function rather than
`world.ledger_for(name)`; `northstar_contracts.World` has no such method, and
attribution is recorded at the boundary here because the ledger stores what
happened rather than who did it. Chapter 17 threads a trace parent through
every crossing instead, which is the version that scales.

The chapter's `orchestrator.py` excerpt appends findings to
`lead.state.messages`. `AgentLoop` has no `.state`; this uses
`start()`, `RunState.with_messages(...)`, `resume()`, which is the same
sequence against the real API.
