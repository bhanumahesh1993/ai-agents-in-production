# Chapter 16 — a labelled failure catalog and three detectors

**What this artifact proves:** a failure mode named in a taxonomy becomes an
automated regression test through a fully mechanical path — label, measure
agreement, cluster, detect, calibrate, promote — and the trustworthiness of
that test is a number you measure rather than a property you assume. Two of
the three detectors here clear the precision floor and may block a release.
The third does not, and the reason it does not is printed.

## Run it

```bash
python artifacts/ch16-failures/demo.py
```

The demo prints, in order:

1. **MAST**: three categories, fourteen modes, at the prevalences reported for
   the published corpus — 41.8% specification and system design, 36.9%
   inter-agent misalignment, 21.3% task verification and termination.
2. **This catalog's distribution**, which is a different ranking, because the
   published prevalences are a prior and not a forecast.
3. **Inter-annotator agreement** on the primary label, as Cohen's kappa, with
   every disagreement named. Each one is a disambiguation rule the labelling
   guide still owes.
4. **The per-detector precision, recall and false-positive table**, and which
   detectors that table allows into the release gate.
5. **Clustering inside the largest mode**, because twenty traces in one mode
   are not twenty bugs.
6. **The Chapter 1 trace**, with `detect_unverified_write` firing on a run
   whose status is `succeeded`.
7. **The regression assertions** that turn each confirmed detection into a
   test.

The demo exits non-zero if a detector in the release gate falls below its
precision floor, if no detector clears it, if a regression assertion no longer
holds, if the verification detector stops firing on the Chapter 1 trace, or if
the catalog's annotator agreement is degenerate.

## Files

| File | What it is |
|---|---|
| `modes.py` | The fourteen MAST modes in their three categories with the published prevalences, plus the three `LOCAL-*` extensions, each with a stated mechanism and its Northstar shape. |
| `catalog.py` | `FailureLabel`, `Trace`, the twenty-six scenarios, and the loader for the annotations. Traces are produced by running scripted agents, not stored as blobs. |
| `labels/annotations.json` | Two independent annotators plus the adjudicated result, every label carrying evidence step indices. |
| `detectors/repetition.py` | FM-1.3. Reads the log, canonicalises, counts. `limit=2` on purpose. |
| `detectors/verification.py` | FM-3.2. A write committed, success reported, nothing read the world back. The detector that would have caught Chapter 1. |
| `detectors/termination.py` | FM-1.5. The run consumed its whole allowance and never reached a terminal state of its own. |
| `calibrate.py` | Cohen's kappa, `detector_report`, the clustering, the promotion rule, and the regression assertions. |
| `demo.py` | Runs all of it and asserts the properties. |
| `test_ch16.py` | The same properties as assertions, including each detector silent on the trace it should be silent on. |
| `conftest.py` | Module isolation when the whole `artifacts/` tree runs under one pytest. |

## Read `calibrate.py` first

Two numbers decide whether any of this is measurement or opinion.

**Cohen's kappa** on the primary label, corrected for the agreement two people
would reach by guessing with the same marginal frequencies. This catalog comes
in around 0.71, and the five runs the annotators disagreed on are printed
rather than averaged away. Two of them are the confusions the chapter names:
step repetition against task derailment on a long meandering run, and
premature termination against disobeying the task specification whenever the
agent stopped early. Low agreement is a finding, not a reason to stop.

**Precision on the labelled catalog** for every detector, with the
false-positive rate measured against the *clean* subset — which is why the
catalog contains successful, unlabelled runs at all. The promotion rule is a
single line: a detector graduates into the release gate when its precision
exceeds 0.90. Until then it runs in report-only mode and its findings go into
the next labelling round.

## Why one detector deliberately falls short

`detect_step_repetition` measures a precision of 0.889 on this catalog, so it
does **not** graduate. The run it gets wrong is `nr-run-24`: a correct run
that reads the order, does a stretch of unrelated work, and re-acquires the
order once before writing. That is ordinary behaviour in a long run, the
annotators labelled it clean, and the detector fires anyway.

That trace is in the catalog on purpose. A detector shipped without a measured
false-positive rate is an alert channel with an unknown signal-to-noise ratio,
and the observable outcome is a team that mutes it while believing the mode is
covered. Raising `limit` from 2 to 3 would clear the floor and would also stop
the detector catching the real cases at three repeats; that trade-off is a
tuning decision somebody has to make with the numbers in front of them, which
is what the table is for.

## What the regression assertions check, and in both directions

A trace a detector fired on, once a human confirms the label, becomes a case
with an assertion attached: this scenario, replayed against the current agent
version, must not trigger this detector. `calibrate.REGRESSIONS` holds three
of them, each pairing the trace that found the mode with the repaired version
of the same scenario.

`check_regressions` asserts **both** directions: the repaired trace must be
clean *and* the broken trace must still fire. A suite that only asserted the
fix would go green the day the detector stopped working, and the mode would
then be uncovered while the suite reported success. `test_ch16.py` proves the
suite notices by handing it a set of traces where the detectors have been
quietly disarmed.

## Two places the code differs from the chapter's excerpts

**Event payload field names.** The chapter's excerpt reads `call["name"]`; this
repository's event log emits `payload["tool"]` alongside `arguments`,
`call_id`, and `attempt`. The detectors use the field names the runtime
actually emits, because a detector that reads a field nobody writes is a
detector that silently never fires.

**`FM-3.1` has no detector, and that is the finding.** Premature termination is
the mirror image of FM-1.5 — the same missing specification producing the
opposite behaviour — and a run that stops early is structurally identical to a
run that finished. Only a state grader distinguishes them. `modes.py` records
that in each mode's `detectable` field, so the gap is visible in the taxonomy
rather than discovered when somebody asks why the mode is never counted.
