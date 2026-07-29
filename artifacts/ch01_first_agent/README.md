# Chapter 1 — a hundred-line loop, and a double refund

**What this artifact proves:** a run that reports `succeeded` with a clean trace
is not evidence that the world is in the intended state, and the smallest
sufficient repair is an idempotency key *derived* from the run and step and
enforced by the target system rather than by the agent's judgment.

## Run it

```bash
make demo-ch01
# or
python -m artifacts.ch01_first_agent.demo
```

The demo runs the same agent, on the same goal, against the same injected fault,
twice:

1. **Broken.** `issue_refund` is called without an idempotency key. The refund
   service commits the write and *then* times out on the way back. The agent sees
   a retryable failure and calls the tool again. Ledger: two refunds. Run status:
   `succeeded`.
2. **Repaired.** The same trajectory, with `idempotency_key(run_id, step_id)`
   passed on the call. The second attempt observes the first attempt's outcome.
   Ledger: one refund. Run status: `succeeded`.

Both runs report success. Only one of them is telling the truth. That is the
whole point of the chapter, and of the book.

The demo exits non-zero if the broken run does *not* double-refund or the
repaired run does, so it doubles as a regression test for the fault injector.

## Files

| File | What it is |
|---|---|
| `loop.py` | The provider-agnostic loop, written out longhand — roughly a hundred lines, no framework. The same shape `northstar_runtime.AgentLoop` implements with the production concerns added. |
| `tools_broken.py` | `issue_refund` as Northstar first wrote it: retries on timeout, no key. |
| `tools_repaired.py` | The same tool with a derived key. The diff is two lines. |
| `demo.py` | Runs both, prints the trajectory and the resulting side-effect ledger, and asserts the difference. |
| `test_ch01.py` | The same assertions as a test. |

## Read `loop.py` first

It is deliberately unglamorous. Strip away the framework and an agent is a while
loop that alternates between asking a model what to do and doing it. Three
details in it matter more than the rest:

- Tool results are appended back into the message list. That is the *observe*
  half of reason-act-observe, and it is why context grows.
- The absence of tool calls is the stopping condition. The model decides when the
  run ends.
- The turn limit **raises** rather than returning. Silently truncating a run and
  reporting success is precisely the failure this chapter is about.
