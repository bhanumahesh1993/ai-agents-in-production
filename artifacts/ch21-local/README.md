# Chapter 21 — the local stack, on one machine, with no credentials

**What this artifact proves:** the complete agent system — tool boundary, policy
enforcement, event log, and authoritative world — runs on one machine with no
credentials and no network, and a specific partial failure is reproducible on
demand rather than waited for. Three attempts at `issue_refund`, one refund in
the ledger, every time.

## Run it

```bash
make demo-ch21
# or
python artifacts/ch21-local/demo.py

make local-up        # the nine containers; needs Docker, and only this does
```

The demo validates the Compose file by parsing it, checks the model modes, the
cassette policy, and the fault catalogue, then runs the damaged-item task end to
end in mock mode with the timeout fault injected:

```text
gateway    listening: in-process MCP 2025-11-25  (6 tools)
agent      admission ok  run_ch21_local  tenant=northstar
worker   run_ch21_local step=3  tool.called   issue_refund
worker   run_ch21_local step=3  tool.result   ToolTimeout: issue_refund timeout
worker   run_ch21_local step=3  tool.called   issue_refund  [attempt 2]
worker   run_ch21_local step=3  tool.result   ok  [attempt 2]
ledger     NR-2026-0041827  refunds=1  total_cents=3250
```

It exits non-zero if the ledger holds more than one refund, if the injected
timeout produced no retry to survive, if mock and replay leave different worlds,
if a service or an image pin is missing from the Compose file, or if a cassette
carries an unredacted key or has no expiry.

### One honest deviation, stated rather than papered over

The chapter says `make demo-ch21` brings the Compose stack up. **This demo does
not start Docker.** No chapter demo in this repository may require a daemon, a
network, or a credential, and CI runs with none of the three. So the nine
services ship as a real Compose file validated by *parsing*, and the task runs
in process against the same MCP gateway, the same shared policy bundle, and the
same world the composed stack would use. `make local-up` is the only command
here that needs containers, and it is the one thing you cannot verify offline.

Parsing is a weaker check than applying, and it is not a token one. It catches a
service that quietly disappeared, an image referenced but never pinned, a
floating tag, a dependency on a service that does not exist, and the agent
losing its Postgres — which is most of what actually breaks a Compose file
between one engineer's laptop and the next.

## Files

| File | What it is |
|---|---|
| `compose.yaml` | The nine services, every image a digest-pinned variable. Each stands in for a production component, so Chapter 22 is a substitution rather than a rewrite. |
| `.env.example` | The image digests, and the `MODEL_MODE=mock` default. |
| `stack.py` | The Compose parser and the checks over it. No YAML dependency: a subset loader that raises rather than guessing. |
| `mcp_server.py` | The local MCP gateway. JSON-RPC 2.0 over stdio and in process, `initialize`/`tools/list`/`tools/call`, with the **shared** policy bundle evaluated before any tool runs. |
| `model_mode.py` | `model_for_mode()`, the four modes, and `RecordingModel` — the only class here that is not part of the shared runtime contract. |
| `scripts/refund.json` | The hand-written script. The fourth step is the retry, stated rather than hoped for. |
| `scripts/refund.jsonl` | The recorded cassette, redacted and stamped, that `replay` loads. |
| `cassettes.py` | Provenance, the ninety-day shelf life, and `unredacted()` for CI. |
| `faults.py` | Six catalogued failures, each with a distinct correct response, and an honest list of the two this world cannot produce. |
| `local_model.py` | The opt-in local-inference overlay and the ten promotion checks. Imports no SDK and reads no variable at import time. |
| `run_local.py` | The damaged-item task on the stack, and everything worth asserting on. |
| `demo.py` | All of the above, with a non-zero exit on a second refund. |
| `test_ch21.py` | The stack, the gateway, the modes, the cassettes, and the faults. |
| `tests/test_refund_once.py` | The test the chapter is built around, with its negative control. |
| `conftest.py` | Module isolation when all of `artifacts/` runs under one pytest. |

## Read `tests/test_refund_once.py` first

Nine lines of setup and one assertion:

```python
world.inject_fault("issue_refund", kind="timeout")
model = FakeModel(default=[..., ToolCall(id="2", name="issue_refund",
                                         arguments=PAY),
                           ToolCall(id="3", name="issue_refund",
                                    arguments=PAY), ...])
loop = AgentLoop(model, registry_for(world), max_turns=8)
loop.run("Customer reports a cracked lamp shade.", run_id=RUN_ID)
assert [r.amount_cents for r in world.refunds_for(ORDER)] == [3250]
```

The third `ToolCall` is the point: the retry is not hoped for, it is stated. The
model is no longer the system under test — the refund path is. The file ships
the negative control beside it, because a test that cannot fail proves nothing:
the same run with the key removed asserts `[3250, 3250]`.

## Three places the code deviates from the obvious

**The gateway runs the shared policy bundle, colons and all.** The chapter's
prose names the `refunds.write` scope; `northstar_policy.default_northstar_policy`
spells it `refunds:write`. The artifact uses the shared bundle unchanged rather
than forking it to match the prose, because the policy bundle is on the
must-be-identical list and a local fork of it is exactly what makes a green
local run mean nothing.

**Tools registered against the gateway raise if they ever execute locally.**
`GatewayRegistry` registers each spec against `_unreachable`, which raises. If
that function ever runs, something bypassed the enforcement point, and a
bypassable enforcement point is not one. `test_the_registry_cannot_be_bypassed`
asserts it.

**Two catalogued faults are named and not producible.** `expired_token` and
`partial` need a real token boundary and a multi-row write, which Chapters 19
and 24 have and this fixture does not. `faults.apply()` raises
`NotImplementedError` for both rather than silently substituting a different
failure, because a catalogue that quietly maps four failures onto one fixture
behaviour tells you your agent handles cases it has never met.
