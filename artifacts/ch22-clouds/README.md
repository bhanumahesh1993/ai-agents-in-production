# Chapter 22 — one agent, three clouds, one scorecard

**What this artifact proves:** the portable core is genuinely portable — one
agent, three adapters, no changes to the loop, the tools, the policy, or the
graders — and a cross-cloud comparison is only meaningful when the workload, the
auth boundary, and the success definition are held identical, with unmeasured
values reported as unmeasured.

## Run it

```bash
make demo-ch22
# or
python artifacts/ch22-clouds/demo.py
```

The demo runs the full scorecard against the **mock** adapter, so you can see
the comparison mechanics with no account on anything. Then it shows a platform
that verifies nothing reporting `cents_per_verified_success` as undefined rather
than as free, the three real adapters answering the pure half of the interface
offline, the Azure publishing gap as a value rather than a paragraph, and the
three Terraform overlays agreeing on the 5,000-cent threshold.

It exits non-zero if the portable core reaches past the four methods, if the
overlays disagree about the threshold, if a real adapter's `session_store()`
does anything other than fail with a named install command, if an adapter
collapses the user and the agent into one identity, or if the scorecard reports
a cold start nobody measured.

## Files

| File | What it is |
|---|---|
| `adapters/base.py` | The four-method `CloudAdapter` protocol, `CloudUnavailable`, `ExitCost`, and the `PORTABLE` list. |
| `adapters/mock.py` | The adapter the demo and the suite run against. Not a simulation of any platform, and `cold_start_ms()` returns `None` for exactly that reason. |
| `adapters/aws.py` | Bedrock AgentCore. The Gateway chain, the eight-hour session ceiling, and the IAM-or-JWT principal mapping. |
| `adapters/gcp.py` | The Gemini Enterprise Agent Platform. SPIFFE-based identity, the seven-day operation window, and the preview count when the design leans on the Agent Identity API. |
| `adapters/azure.py` | Foundry Agent Service. Entra identities, the thirty-day resumable envelope, and `published_identity_gap()`. |
| `portable.py` | The core: `build_loop()` makes exactly four adapter calls, `run_once()` runs and grades one task. |
| `tasks.py` | Four tasks, held identical across platforms. One is designed to be refused; one exists so a fixture with a single right answer cannot pass. |
| `scorecard.py` | `CloudScore` with its ten fields, the harness, `compare()`, and the renderer. |
| `iac/{aws,gcp,azure}/main.tf` | The overlays. Each header states exactly what it creates and what it costs while it exists. |
| `iac.py` | The HCL subset parser and the checks over it. |
| `demo.py` | All of the above, with a non-zero exit on any of them drifting. |
| `test_ch22.py` | Twenty-one assertions, including the one that protects the portability claim. |
| `conftest.py` | Module isolation when all of `artifacts/` runs under one pytest. |

## Read `adapters/base.py` first, then `test_ch22.py`

Four methods:

```python
class CloudAdapter(Protocol):
    name: str

    def session_store(self) -> Checkpointer: ...
    def tool_endpoint(self) -> str: ...
    def principal_for(self, inbound: dict) -> Principal: ...
    def exporter(self) -> str: ...
```

If an adapter needs a fifth, that is a signal the platform is reaching into a
plane you meant to keep portable, and it goes in the exit-cost note rather than
into the interface.

`test_the_portable_core_calls_only_the_four` is the assertion that keeps the
claim honest. It hands the core an adapter that records every attribute access
and asserts the recorded set is a subset of the four. An adapter may expose more
— the scorecard asks for `cold_start_ms()` and `preview_dependencies()` — but
the *core* must not reach for them, or the extra becomes load-bearing without
anyone deciding that it should.

## Two fields carry the scorecard

`cold_start_ms` is `None` rather than a vendor figure when nobody measured one.
A missing measurement is information; a borrowed one is not. All four adapters
here return `None`, and `test_the_scorecard_never_guesses_a_cold_start` fails
the day one of them starts guessing.

`cents_per_verified_success` divides by *graded* successes, not by invocations,
because a platform that is cheap per call and fails more often is not cheap.
When nothing was graded a success the field is `UNDEFINED_COST` (`-1`) rather
than a number: not zero, which would read as free, and not the total, which
would read as a rate.

## Three things this artifact does not do, and says so

**It does not deploy anything.** No chapter demo may create a cloud resource,
need a credential, or import a cloud SDK. The three real adapters are readable,
importable, and offline; `session_store()` is the one method of the four that
genuinely needs an account, and it raises `CloudUnavailable` with the install
command named. That is not a coincidence — the session store is also the least
portable thing any of these platforms sells.

**It does not measure any platform.** The scorecard's `p50_ms` and `p95_ms` are
real measurements of the harness's own overhead against a mock adapter, which is
what a mock adapter can honestly produce, and the demo says so. Numbers for AWS,
Google Cloud, or Azure require applying the overlays in your own account, in
your region, with your container and your model, which is the only way the
chapter's rule — *the same agent, deployed the same way, measured with the same
scorecard* — can produce a number worth having.

**It validates the overlays by parsing, not by applying.** `iac.py` covers the
HCL subset the three `main.tf` files use and raises on anything else. It is not
an HCL implementation and should not grow into one. What it catches is the drift
that matters here: one platform's overlay quietly enforcing a different approval
threshold, a variable declared and never used, or an overlay whose tool endpoint
is not an output anyone can read.
