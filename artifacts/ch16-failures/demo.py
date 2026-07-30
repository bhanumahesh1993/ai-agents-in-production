"""Label the catalog, run the detectors, and measure the detectors.

    python artifacts/ch16-failures/demo.py

Prints the three MAST categories with the prevalences reported for their
corpus, the labelled distribution of *this* catalog, the inter-annotator
agreement that says whether those counts are an instrument or an opinion, the
per-detector precision and false-positive table, the clustering inside the
largest mode, and the regression assertions that promote a confirmed detection
into a test. Exits non-zero if any detector promoted into the release gate
falls below its precision floor, or if a regression assertion no longer holds.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import calibrate
import catalog
import detectors as detector_suite
import modes as taxonomy

CHAPTER_ONE_RUN = "nr-run-09"       # a refund with no read-back


def print_taxonomy() -> None:
    """Three categories, fourteen modes, and the reported prevalences."""
    print(f"{'category':<38}{'modes':>6}{'prevalence':>12}")
    total = 0
    for category in taxonomy.CATEGORIES:
        found = taxonomy.modes_in(category.key)
        total += len(found)
        print(f"{category.title:<38}{len(found):>6}"
              f"{category.prevalence:>11.1%}")
    share = sum(c.prevalence for c in taxonomy.CATEGORIES)
    print(f"{'MAST total':<38}{total:>6}{share:>11.1%}")
    print(f"{'Northstar LOCAL-* extensions':<38}"
          f"{len(taxonomy.LOCAL_MODES):>6}{'-':>11}")
    print("\nThose prevalences are properties of the published corpus, not")
    print("of this catalog. They say where to look first. The next table is")
    print("what this system actually does.")


def print_distribution(labels: list[catalog.FailureLabel]) -> None:
    """The ranked list, which is the input to every prioritisation."""
    counts = calibrate.distribution(labels)
    total = sum(counts.values())
    print(f"\n{'primary mode':<26}{'runs':>6}{'share':>8}   title")
    for mode_id, count in counts.items():
        print(f"{mode_id:<26}{count:>6}{count / total:>8.1%}   "
              f"{taxonomy.MODES[mode_id].title}")


def print_agreement(labels: list[catalog.FailureLabel]) -> float:
    """Cohen's kappa, and the pairs the annotators could not separate."""
    first = catalog.primary_labels(labels, "annotator-a")
    second = catalog.primary_labels(labels, "annotator-b")
    kappa = calibrate.cohens_kappa(first, second)
    disagreements = sorted(
        run
        for run in set(first) | set(second)
        if first.get(run, "NONE") != second.get(run, "NONE")
    )
    print(f"\ninter-annotator agreement (primary label): kappa = {kappa:.3f}")
    print(f"disagreed on {len(disagreements)} of "
          f"{len(set(first) | set(second))} labelled runs:")
    for run in disagreements:
        print(f"  {run}: a said {first.get(run, 'NONE')}, "
              f"b said {second.get(run, 'NONE')}")
    print("Each of those pairs is a disambiguation rule the labelling guide")
    print("still owes. The guide is the artifact that makes a published")
    print("taxonomy yours.")
    return kappa


def print_calibration(
    reports: dict[str, dict[str, float]],
) -> set[str]:
    """One row per detector, and which of them may block a release."""
    print(f"\n{'detector':<34}{'mode':<10}{'fired':>6}{'prec':>7}"
          f"{'recall':>8}{'fp rate':>9}   gate")
    for name, mode, numbers, is_promoted in calibrate.report_rows(reports):
        gate = "release" if is_promoted else "report-only"
        print(f"{name:<34}{mode:<10}{numbers['fired']:>6.0f}"
              f"{numbers['precision']:>7.3f}{numbers['recall']:>8.3f}"
              f"{numbers['fp_rate']:>9.3f}   {gate}")
    graduated = calibrate.promoted(reports)
    print(f"\nA detector graduates at precision > "
          f"{calibrate.PRECISION_FLOOR:.2f}. The one below the floor is not")
    print("weak: it fires on a legitimate re-acquisition after a long")
    print("stretch of unrelated work, which is ordinary behaviour. It runs")
    print("in report-only mode and its findings go into the next round.")
    return graduated


def print_clusters(
    labels: list[catalog.FailureLabel],
    traces: list[catalog.Trace],
) -> None:
    """Twenty traces in one mode are not twenty bugs."""
    counts = calibrate.distribution(labels)
    biggest = next(iter(counts))
    clusters = calibrate.cluster_within(biggest, labels, traces)
    print(f"\nclustering inside {taxonomy.describe(biggest)}:")
    for scenario, runs in clusters.items():
        print(f"  {len(runs):>2} runs  {scenario}")
    print("Same mode, two mechanisms, two different fixes. A single")
    print("aggregate count would have sent the team to whichever they")
    print("thought of first.")


def print_chapter_one(traces: list[catalog.Trace]) -> bool:
    """The Chapter 1 shape: succeeded, and nothing checked."""
    trace = catalog.trace_by_id(traces, CHAPTER_ONE_RUN)
    label = detector_suite.run_detector("detect_unverified_write", trace)
    print(f"\n{trace.run_id}: status={trace.status!r}, "
          f"refund rows={trace.refunds}, final text "
          f"{trace.final_text[:38]!r}")
    if label is None:
        print("  detect_unverified_write did not fire")
        return False
    print(f"  {label.mode} at step(s) {list(label.evidence_steps)}: "
          f"{label.note}")
    print("  The run reported success. Nothing in it compared that claim")
    print("  against the ledger, so one refund and two would have looked")
    print("  identical from inside the agent.")
    return True


def main() -> int:
    """Label, detect, calibrate, cluster, and assert the regressions."""
    traces = catalog.build_traces()
    labels = catalog.load_labels()

    print("=== MAST: three categories, fourteen modes ===")
    print_taxonomy()

    print(f"\n=== this catalog: {len(traces)} recorded runs ===")
    print_distribution(labels)
    kappa = print_agreement(labels)

    print("\n=== detector calibration against the labelled catalog ===")
    reports = calibrate.calibrate_all(traces, labels)
    graduated = print_calibration(reports)

    print_clusters(labels, traces)

    print("\n=== the Chapter 1 trace, replayed ===")
    fired_on_chapter_one = print_chapter_one(traces)

    print("\n=== regression assertions ===")
    problems = calibrate.check_regressions(traces)
    for check in calibrate.REGRESSIONS:
        print(f"  {check.describe()}")
    if problems:
        for problem in problems:
            print(f"  BROKEN: {problem}")

    failures: list[str] = []
    for name in graduated:
        if reports[name]["precision"] <= calibrate.PRECISION_FLOOR:
            failures.append(
                f"{name} is in the release gate at precision "
                f"{reports[name]['precision']:.3f}"
            )
    if not graduated:
        failures.append("no detector cleared the precision floor")
    if problems:
        failures.append(f"{len(problems)} regression assertion(s) failed")
    if not fired_on_chapter_one:
        failures.append(
            "detect_unverified_write should fire on the Chapter 1 trace"
        )
    if not 0.0 < kappa < 1.0:
        failures.append(
            f"agreement of {kappa:.3f} is degenerate; the catalog should "
            "carry real disagreement for the adjudication step to mean "
            "anything"
        )

    print("\n--- what this proves ---")
    print("A failure mode named in a taxonomy becomes an automated")
    print("regression test through a fully mechanical path, and the")
    print("trustworthiness of that test is a number you measure rather")
    print("than a property you assume.")

    if failures:
        print("\nFAILED:")
        for line in failures:
            print(f"  - {line}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
