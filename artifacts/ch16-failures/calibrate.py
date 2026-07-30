"""Agreement, precision, and the rule that promotes a detector into a gate.

Two numbers decide whether any of this is measurement or opinion.

**Cohen's kappa** between two independent annotators, on the primary label,
corrected for the agreement two people would reach by guessing with the same
marginal frequencies. Low agreement is not a reason to abandon the exercise;
it is a finding, and it almost always points at two or three mode pairs your
team cannot distinguish. Adjudicate those, write a one-line disambiguation
rule for each into the labelling guide, and re-label.

**Precision on the labelled catalog** for every detector. A detector with a
30% false-positive rate is not a weak detector, it is a detector that will be
turned off within a week, and the mode it covers will then be uncovered while
everyone believes it is monitored. Thresholds go by consequence: a detector
that opens an investigation can run loose, and one that blocks a release must
run tight.

Northstar's promotion rule, which generalises: a detector graduates into the
release gate when its precision on the labelled catalog exceeds 0.90 and it
has fired zero times on the golden set. Until then it runs in report-only mode
and its findings go into the next labelling round.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

import catalog
import detectors as detector_suite
from catalog import ADJUDICATED, FailureLabel, Trace

__all__ = [
    "PRECISION_FLOOR",
    "REGRESSIONS",
    "RegressionCheck",
    "build_regression_traces",
    "calibrate_all",
    "check_regressions",
    "cluster_within",
    "cohens_kappa",
    "detector_report",
    "distribution",
    "promoted",
]

#: Precision a detector needs before it is allowed to block a release. The
#: second-order cost of a false positive here is not a wasted afternoon; it
#: is a team that learns to override the gate.
PRECISION_FLOOR = 0.90


def detector_report(
    fired: set[str],
    labeled: set[str],
    all_runs: set[str],
) -> dict[str, float]:
    """Precision, recall, and FP rate against the catalog.

    Args:
        fired: Runs this detector flagged.
        labeled: Runs the adjudicated pass labelled with this mode.
        all_runs: Every run in the catalog.

    Returns:
        Four numbers. ``fp_rate`` is measured against the *clean* subset,
        which is the number that predicts whether anyone will keep the
        detector switched on.
    """
    tp = len(fired & labeled)
    fp = len(fired - labeled)
    clean = all_runs - labeled
    return {
        "fired": float(len(fired)),
        "precision": tp / len(fired) if fired else 0.0,
        "recall": tp / len(labeled) if labeled else 0.0,
        "fp_rate": fp / len(clean) if clean else 0.0,
    }


def cohens_kappa(
    first: dict[str, str],
    second: dict[str, str],
) -> float:
    """Agreement between two annotators, corrected for chance.

    Args:
        first: Run id to primary mode, for one annotator.
        second: The same, for the other. Runs missing from either map are
            treated as ``"NONE"``: choosing not to label something is a
            judgment, and excluding it would quietly inflate agreement.

    Returns:
        Cohen's kappa. ``1.0`` is perfect agreement, ``0.0`` is what two
        people guessing with these marginals would reach.

    Raises:
        ValueError: If there is nothing to compare.
    """
    runs = sorted(set(first) | set(second))
    if not runs:
        raise ValueError("cannot compute agreement over zero runs")

    a = [first.get(r, "NONE") for r in runs]
    b = [second.get(r, "NONE") for r in runs]
    n = len(runs)

    observed = sum(1 for x, y in zip(a, b, strict=True) if x == y) / n
    categories = set(a) | set(b)
    expected = sum(
        (a.count(c) / n) * (b.count(c) / n) for c in categories
    )
    if expected == 1.0:
        return 1.0
    return (observed - expected) / (1.0 - expected)


def distribution(
    labels: Sequence[FailureLabel],
    annotator: str = ADJUDICATED,
) -> dict[str, int]:
    """Primary-label counts, most common first. The input to a ranking."""
    counts: dict[str, int] = {}
    for label in labels:
        if label.annotator != annotator or not label.primary:
            continue
        counts[label.mode] = counts.get(label.mode, 0) + 1
    return dict(sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])))


def cluster_within(
    mode: str,
    labels: Sequence[FailureLabel],
    traces: Sequence[Trace],
    annotator: str = ADJUDICATED,
) -> dict[str, list[str]]:
    """Group one mode's traces by the concrete thing that varies.

    Twenty labelled traces in one mode are not twenty bugs. An aggregate
    count sends the team to fix whichever cluster they happened to read
    first, and the number barely moves. Grouping here is by scenario,
    which is the shape of "which tool, which step position, which intent"
    that this catalog carries.
    """
    scenarios = {t.run_id: t.scenario for t in traces}
    clusters: dict[str, list[str]] = {}
    for label in labels:
        if label.mode != mode or label.annotator != annotator:
            continue
        key = scenarios.get(label.run_id, "unknown")
        clusters.setdefault(key, []).append(label.run_id)
    return {k: sorted(v) for k, v in sorted(clusters.items())}


def calibrate_all(
    traces: Sequence[Trace],
    labels: Sequence[FailureLabel],
) -> dict[str, dict[str, float]]:
    """One report per detector, over the whole catalog."""
    all_runs = {t.run_id for t in traces}
    results = detector_suite.run_all(list(traces))
    out: dict[str, dict[str, float]] = {}
    for name, mode in detector_suite.DETECTORS.items():
        out[name] = detector_report(
            detector_suite.fired(results[name]),
            catalog.labelled(list(labels), mode),
            all_runs,
        )
    return out


def promoted(reports: dict[str, dict[str, float]]) -> set[str]:
    """Detectors whose precision clears the floor for the release gate."""
    return {
        name
        for name, report in reports.items()
        if report["precision"] > PRECISION_FLOOR
    }


# ------------------------------------------------------- regression suite


@dataclass(frozen=True)
class RegressionCheck:
    """One confirmed detection, promoted into the regression suite.

    A trace that a detector fired on, once a human confirmed the label,
    becomes a replay case with an assertion attached: this scenario,
    replayed against the current agent version, must not trigger this
    detector. That assertion is the durable form of the fix. Prompts get
    rewritten, models get upgraded, topologies get simplified, and the
    assertion outlives all of them.
    """

    detector: str
    broken_run: str
    repaired_run: str
    fix: str

    def describe(self) -> str:
        """One line for the report."""
        return (
            f"{self.detector}: {self.broken_run} -> {self.repaired_run} "
            f"({self.fix})"
        )


#: The three fixes, each anchored to the trace that found the mode.
REGRESSIONS: tuple[RegressionCheck, ...] = (
    RegressionCheck(
        "detect_step_repetition",
        "nr-run-06",
        "nr-fix-repetition",
        "the order is read once and the result is carried forward",
    ),
    RegressionCheck(
        "detect_unverified_write",
        "nr-run-09",
        "nr-fix-verification",
        "the ledger is read back after the last write",
    ),
    RegressionCheck(
        "detect_termination_unawareness",
        "nr-run-13",
        "nr-fix-termination",
        "an above-threshold claim now has a terminal state: escalate",
    ),
)


def build_regression_traces() -> list[Trace]:
    """Run the repaired version of each scenario the detectors found."""
    return [
        catalog.record_scenario(*scenario)
        for scenario in catalog.FIXED_SCENARIOS
    ]


def check_regressions(
    catalog_traces: Sequence[Trace] | None = None,
    fixed_traces: Sequence[Trace] | None = None,
) -> list[str]:
    """Assert every fix still holds, and every finding still reproduces.

    Returns:
        A list of failure descriptions, empty when the suite is green. A
        regression suite that only asserts the fix would go green the day
        the detector stopped working, so both directions are checked: the
        broken trace must still fire and the repaired one must not.
    """
    broken = list(catalog_traces or catalog.build_traces())
    fixed = list(fixed_traces or build_regression_traces())
    problems: list[str] = []

    for check in REGRESSIONS:
        before = catalog.trace_by_id(broken, check.broken_run)
        after = catalog.trace_by_id(fixed, check.repaired_run)
        if detector_suite.run_detector(check.detector, before) is None:
            problems.append(
                f"{check.detector} no longer fires on {check.broken_run}; "
                "the detector, not the agent, has regressed"
            )
        if detector_suite.run_detector(check.detector, after) is not None:
            problems.append(
                f"{check.detector} fires on the repaired "
                f"{check.repaired_run}: {check.fix} did not hold"
            )
    return problems


def report_rows(
    reports: dict[str, dict[str, float]],
) -> list[tuple[str, str, dict[str, float], bool]]:
    """``(detector, mode, numbers, promoted)`` rows for a printed table."""
    graduated = promoted(reports)
    return [
        (name, detector_suite.DETECTORS[name], reports[name],
         name in graduated)
        for name in sorted(reports)
    ]
