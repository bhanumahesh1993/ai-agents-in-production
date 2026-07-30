"""Chapter 16's claims, as assertions.

The taxonomy is fourteen modes in three categories with the published
prevalences; the catalog is stratified and includes successful runs; agreement
is computed rather than assumed; every detector's precision is measured before
it is allowed near a release gate; and a confirmed detection becomes a
regression assertion that fails in both directions.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import calibrate
import catalog
import detectors as detector_suite
import modes as taxonomy
from detectors.repetition import detect_step_repetition
from detectors.termination import detect_termination_unawareness
from detectors.verification import detect_unverified_write

TRACES = catalog.build_traces()
LABELS = catalog.load_labels()


# ------------------------------------------------------------ the taxonomy


def test_mast_is_fourteen_modes_in_three_categories() -> None:
    """Five, six, three, at 41.8 / 36.9 / 21.3 percent."""
    assert len(taxonomy.MAST_MODES) == 14
    assert len(taxonomy.CATEGORIES) == 3

    sizes = [len(taxonomy.modes_in(c.key)) for c in taxonomy.CATEGORIES]
    assert sizes == [5, 6, 3]

    prevalences = [c.prevalence for c in taxonomy.CATEGORIES]
    assert prevalences == [0.418, 0.369, 0.213]
    assert round(sum(prevalences), 3) == 1.0

    # The modes the chapter names by id all resolve, and to the right
    # category.
    for mode_id, category_key in (
        ("FM-1.3", "specification"),
        ("FM-1.4", "specification"),
        ("FM-1.5", "specification"),
        ("FM-2.4", "misalignment"),
        ("FM-3.1", "verification"),
        ("FM-3.2", "verification"),
        ("FM-3.3", "verification"),
    ):
        category = taxonomy.category_of(mode_id)
        assert category is not None and category.key == category_key


def test_local_modes_are_namespaced_and_resolvable() -> None:
    """Counts stay comparable to the published ones."""
    assert len(taxonomy.LOCAL_MODES) == 3
    for mode in taxonomy.LOCAL_MODES:
        assert taxonomy.is_local(mode.id)
        assert taxonomy.category_of(mode.id) is None
        assert mode.mechanism.endswith(".")
    assert not taxonomy.is_local("FM-1.3")
    assert taxonomy.describe("FM-1.3").startswith("FM-1.3 ")

    try:
        taxonomy.describe("FM-9.9")
    except KeyError as exc:
        assert "unknown mode" in str(exc)
    else:  # pragma: no cover - the point of the test
        raise AssertionError("an unresolvable mode must not be countable")


# -------------------------------------------------------------- the catalog


def test_the_sample_is_stratified_and_includes_successful_runs() -> None:
    """Sampling only failed runs makes unsafe success undetectable."""
    assert len(TRACES) == 26
    statuses = {t.status for t in TRACES}
    assert statuses == {"succeeded", "failed"}

    succeeded = [t for t in TRACES if t.status == "succeeded"]
    assert len(succeeded) > len(TRACES) / 2

    # Two of those successful runs are labelled unsafe success, which is
    # the stratum a failure-only sampling policy would have missed.
    unsafe = catalog.labelled(LABELS, "LOCAL-UNSAFE-SUCCESS")
    assert len(unsafe) == 2
    for run_id in unsafe:
        assert catalog.trace_by_id(TRACES, run_id).status == "succeeded"


def test_every_label_carries_evidence_and_a_resolvable_mode() -> None:
    """A label without a step index is an impression, not a label."""
    assert LABELS
    for label in LABELS:
        assert label.mode in taxonomy.MODES, label.mode
        assert label.evidence_steps, label.run_id
        assert label.annotator in (
            *catalog.ANNOTATORS, catalog.ADJUDICATED
        )

    try:
        catalog.FailureLabel.from_dict(
            {"run_id": "x", "mode": "FM-1.3", "annotator": "a",
             "evidence_steps": []}
        )
    except ValueError as exc:
        assert "evidence" in str(exc)
    else:  # pragma: no cover - the point of the test
        raise AssertionError("a label with no evidence must be refused")


def test_agreement_is_measured_and_the_disagreements_are_nameable() -> None:
    """Counts built on unmeasured agreement are noise dressed as data."""
    first = catalog.primary_labels(LABELS, "annotator-a")
    second = catalog.primary_labels(LABELS, "annotator-b")
    kappa = calibrate.cohens_kappa(first, second)

    assert 0.5 < kappa < 0.9, kappa
    disagreements = {
        run for run in set(first) | set(second)
        if first.get(run, "NONE") != second.get(run, "NONE")
    }
    assert disagreements

    # Perfect agreement is 1.0 and the function says so.
    assert calibrate.cohens_kappa(first, first) == 1.0


# ------------------------------------------------------------ the detectors


def test_step_repetition_needs_more_than_one_re_acquisition() -> None:
    """``limit=2`` on purpose: one re-read after a compaction is normal."""
    trace = catalog.trace_by_id(TRACES, "nr-run-01")
    assert detect_step_repetition(trace.run_id, trace.events) is not None
    # Raise the limit and the same trace stops being a finding, which is
    # what makes the limit a tuning decision rather than a constant.
    assert detect_step_repetition(
        trace.run_id, trace.events, limit=5
    ) is None

    clean = catalog.trace_by_id(TRACES, "nr-run-22")
    assert detect_step_repetition(clean.run_id, clean.events) is None


def test_repeated_writes_belong_to_the_other_detector() -> None:
    """A detector that claims two modes at once cannot be calibrated."""
    trace = catalog.trace_by_id(TRACES, "nr-run-06")
    label = detect_step_repetition(trace.run_id, trace.events)
    assert label is not None
    assert label.mode == "FM-1.3"

    write_steps = {
        int(e["step"]) for e in trace.events
        if e["type"] == "tool.called"
        and e["payload"]["tool"] == "issue_refund"
    }
    assert not (set(label.evidence_steps) & write_steps)


def test_the_verification_detector_is_narrow_and_repeatable() -> None:
    """A write committed, success reported, and nothing read the world."""
    broken = catalog.trace_by_id(TRACES, "nr-run-09")
    first = detect_unverified_write(broken.state, broken.events)
    again = detect_unverified_write(broken.state, broken.events)
    assert first is not None
    assert first == again              # no side effects, no drift
    assert broken.status == "succeeded"

    verified = catalog.trace_by_id(TRACES, "nr-run-22")
    assert detect_unverified_write(verified.state, verified.events) is None

    # A run that already failed does not need this flag on top of it.
    failed = catalog.trace_by_id(TRACES, "nr-run-13")
    assert detect_unverified_write(failed.state, failed.events) is None


def test_termination_unawareness_needs_the_ceiling_to_mean_anything() -> None:
    """"Used every turn" is only a finding relative to how many there were."""
    trace = catalog.trace_by_id(TRACES, "nr-run-13")
    assert detect_termination_unawareness(
        trace.run_id, trace.events, trace.max_turns
    ) is not None
    assert detect_termination_unawareness(
        trace.run_id, trace.events, max_turns=99
    ) is None

    finished = catalog.trace_by_id(TRACES, "nr-run-22")
    assert detect_termination_unawareness(
        finished.run_id, finished.events, finished.max_turns
    ) is None


# ---------------------------------------------------------- the calibration


def test_precision_decides_which_detectors_may_block_a_release() -> None:
    """Every detector ships with a measured false-positive rate."""
    reports = calibrate.calibrate_all(TRACES, LABELS)
    assert set(reports) == set(detector_suite.DETECTORS)

    for name, numbers in reports.items():
        assert numbers["fired"] > 0, name
        assert 0.0 <= numbers["precision"] <= 1.0
        assert 0.0 <= numbers["recall"] <= 1.0
        assert 0.0 <= numbers["fp_rate"] <= 1.0

    graduated = calibrate.promoted(reports)
    assert graduated
    for name in graduated:
        assert reports[name]["precision"] > calibrate.PRECISION_FLOOR

    # The repetition detector fires on a legitimate re-acquisition, so its
    # precision is below the floor and it stays in report-only mode. That
    # is the discipline, not a defect in the catalog.
    assert "detect_step_repetition" not in graduated
    assert reports["detect_step_repetition"]["fp_rate"] > 0.0


def test_detector_report_matches_its_definition() -> None:
    """The arithmetic, checked against the sets rather than against itself."""
    report = calibrate.detector_report(
        fired={"a", "b", "c"},
        labeled={"a", "b", "d"},
        all_runs={"a", "b", "c", "d", "e"},
    )
    assert report["fired"] == 3.0
    assert report["precision"] == 2 / 3       # a, b hit; c missed
    assert report["recall"] == 2 / 3          # d never fired
    assert report["fp_rate"] == 1 / 2         # c of the two clean runs


def test_clustering_splits_one_mode_into_its_mechanisms() -> None:
    """Twenty traces in one mode are not twenty bugs."""
    clusters = calibrate.cluster_within("FM-1.3", LABELS, TRACES)
    assert len(clusters) >= 2
    assert sum(len(v) for v in clusters.values()) == len(
        catalog.labelled(LABELS, "FM-1.3")
    )


# ------------------------------------------------------- the regression suite


def test_a_confirmed_detection_becomes_an_assertion_that_holds_both_ways() -> (
    None
):
    """The fix must hold, and the detector must still work."""
    assert calibrate.check_regressions(TRACES) == []
    assert len(calibrate.REGRESSIONS) == 3

    fixed = calibrate.build_regression_traces()
    for check in calibrate.REGRESSIONS:
        before = catalog.trace_by_id(TRACES, check.broken_run)
        after = catalog.trace_by_id(fixed, check.repaired_run)
        assert detector_suite.run_detector(check.detector, before)
        assert detector_suite.run_detector(check.detector, after) is None


def test_a_regression_suite_that_only_checks_the_fix_would_go_green() -> None:
    """So the suite checks the detector too, and this proves it notices."""
    fixed = calibrate.build_regression_traces()
    repaired_only = [
        t for t in TRACES if t.run_id not in {
            c.broken_run for c in calibrate.REGRESSIONS
        }
    ] + list(fixed)
    problems = calibrate.check_regressions(
        [*repaired_only, *_disarmed_broken_traces()], fixed
    )
    assert problems
    assert any("no longer fires" in p for p in problems)


def _disarmed_broken_traces() -> list[catalog.Trace]:
    """Stand-ins where the mode has been repaired but the label stayed.

    Used to prove the regression suite fails when a *detector* regresses,
    not only when a fix does.
    """
    fixed = calibrate.build_regression_traces()
    out: list[catalog.Trace] = []
    for check, replacement in zip(
        calibrate.REGRESSIONS, fixed, strict=True
    ):
        clean = catalog.trace_by_id(fixed, replacement.run_id)
        out.append(
            catalog.Trace(
                run_id=check.broken_run,
                scenario=clean.scenario,
                state=clean.state,
                status=clean.status,
                final_text=clean.final_text,
                events=clean.events,
                max_turns=clean.max_turns,
                world=clean.world,
                refunds=clean.refunds,
            )
        )
    return out
