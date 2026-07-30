# Chapter 26 — gates, a shadow adapter, and a canary that can be turned off

**What this artifact proves:** a behavioural regression can be blocked before
release by gating on repeated-run reliability and trajectory invariants against
a stored baseline rather than on a single pass; a candidate version's decisions
can be compared against production without executing a single write; and every
rung of the containment ladder can be exercised without a deploy and verified to
stop **in-flight** mutation, which is the failure a real drill usually finds.

## Run it

```bash
make demo-ch26
# or
python artifacts/ch26-cicd/demo.py
```

Six sections, in the order a change moves through them: the configuration hash,
the reliability gate, the trajectory gate, shadow traffic, the canary, and the
kill-switch drill.

The gates are also runnable on their own, exactly as the workflow invokes them:

```bash
cd artifacts/ch26-cicd
python -m gates.reliability --scenarios critical --k 5 --min-pass-k 0.90 \
    --baseline baselines/main.json --max-regression 0.02 --version v9-good
python -m gates.trajectory \
    --forbid "issue_refund before get_policy" \
    --forbid "issue_refund without idempotency_key" \
    --require-state-grader --version v9-unsafe
```

The demo exits non-zero if a gate fails to block a known-bad candidate, if it
blocks a good one, if a shadowed run touches the world, if the canary widens
past an SLO breach, or if any rung of the containment ladder does not do what it
claims.

## The candidates, and what each one is for

| Version | Differs by | Caught by |
|---|---|---|
| `v8` | the deployed baseline | — |
| `v9-good` | prompt text only | nothing; the hash changes and the behaviour does not |
| `v9-marginal` | drifts more | the **baseline comparison**, after clearing an absolute floor |
| `v9-regressed` | drifts much more | the reliability floor *and* the baseline comparison |
| `v9-unsafe` | refunds before reading policy, no idempotency key | the trajectory gate — its world state is correct |
| `v9-generous` | refunds 4,900c instead of 3,250c | the shadow diff, before a single write |

`v9-unsafe` is the one worth sitting with. It reaches exactly the world state the
state grader wants: one refund, right amount, no duplicate. Graded on outcomes
alone it is a clean release. It gets to that state by moving money before it has
read the policy, and with no derived idempotency key on the write, so the next
timeout double-pays. Outcome grading cannot see either. That is what trajectory
invariants are for, and why they assert what must never happen rather than
prescribing a path — an agent that reads the order twice is taking a different
valid route and should not fail a release.

## Files

| File | What it is |
|---|---|
| `versions.py` | `effective_config_hash()` over code, model snapshot, prompt, tool versions, policy, guardrails, and sandbox image, and the six agent versions the gates release against. |
| `deployment.py` | The mock deployment: the critical suite, and `GatedTools`, a registry that consults the containment flags on **every dispatch** and records the *effective* arguments each tool received. |
| `canary.py` | `FLAGS`, the independent switches; `FlagSet`, which answers `allows_run` and `allows_call`; the cohort ladder; and `CanaryController`, which widens while the SLOs hold and contains when they do not. |
| `rollout.py` | Where the SLO readings come from: runs the suite at each rung, grades the read-only rung on decisions and the write rungs on state, and defers over-ceiling tickets rather than counting them as failures. |
| `gates/reliability.py` | `pass^k` over repeated runs, blocked on a floor **and** on regression against `baselines/main.json`. |
| `gates/trajectory.py` | `"A before B"` and `"A without ARG"` invariants, plus `--require-state-grader`. |
| `baselines/main.json` | The stored baseline. Regenerate with `--write-baseline`; that is a reviewed change, not a convenience. |
| `shadow.py` | `ShadowAdapter`: reads pass through, writes are recorded and never executed, and `compare()` diffs two versions on the decisions they would have made. |
| `drill.py` | Pulls every rung of the ladder and measures what each achieved, including the same fleet contained two ways. |
| `runbook.md` | The incident runbook template: five first checks, the containment ladder, the response sequence, recovery semantics per mutation, and the incident record. |
| `.github/workflows/agent-gates.yml` | The workflow, as a file to copy. It also asserts that the gates still **block** a known-bad candidate, so a gate that has quietly stopped working fails the build. |
| `demo.py` | Runs the whole pipeline and asserts every property. |
| `test_ch26.py` | The same properties as assertions. |

## Read `drill.py`'s `in_flight_containment` first

Everything else in this artifact is machinery around one measurement:

```python
deployment = _fleet(enforce=enforce)     # three runs, one turn in
before = deployment.mutations()
deployment.flags.disable("all_mutations", reason="drill")
deployment.finish_all()
after = deployment.mutations()
```

Built with the flags enforced at the action boundary, `after - before` is **0**.
Built with them enforced only at admission — which is what most systems
actually have — it is **3**. Same fleet, same switch, same moment. The
difference is one `if` in `GatedTools.dispatch`, and it is the difference
between a kill switch and a story about one.

## What the numbers here are, and are not

The baseline's `pass^5` is 1.000, because in mock mode the deployed version is
scripted and does not drift: twenty runs of a deterministic agent succeed twenty
times. That is a property of `FakeModel`, not a claim about a real agent. The
candidates carry seeded `FlakyModel` drift, so their numbers move, and the gate
arithmetic — floor, baseline, regression allowance — is identical either way.
Point it at a live model and the baseline stops being 1.000; nothing else
changes.

Every figure the gates print is computed from runs that happened: successes are
graded by a state grader reading the authoritative world, `pass^k` comes from
`northstar_evals.pass_k`, and the confidence intervals are Wilson intervals over
the same counts.
