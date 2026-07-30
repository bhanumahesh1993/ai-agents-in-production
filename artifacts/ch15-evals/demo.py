"""Two runs, four graders, and the gate that reads them.

    python artifacts/ch15-evals/demo.py

Runs the chapter's opening: Run A, which is *better* than the recorded
reference and which exact-trajectory matching rejects, and Run B, which
reaches the right final state by a path nobody would have approved and which
outcome-only grading accepts. Then runs the six unsafe-success detectors, then
the two-tier gate. Exits non-zero if any of those properties does not hold or
if the gate fails.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import cases
import gate as gating
from cases import REFERENCE_TRAJECTORY, by_family, by_id, run_case
from detectors import DETECTORS, run_detectors
from graders.trajectory import exact_match, tool_calls

CASE_ID = "refund-damaged-partial-04"


def verdicts(
    plan: object,
    label: str,
    faults: tuple[tuple[str, str, int], ...] | None = None,
) -> dict[str, object]:
    """Run one plan against the case and grade it four ways."""
    case = by_id(CASE_ID)
    run = run_case(
        case, plan=plan, faults=faults      # type: ignore[arg-type]
    )
    grades = cases.grade(run)
    names = [c.name for c in tool_calls(run.state)]
    flags = run_detectors(
        run.events,
        in_scope_orders=[case.order_id],
        final_text=run.state.final_text or "",
    )
    return {
        "label": label,
        "names": names,
        "exact": exact_match(names, REFERENCE_TRAJECTORY),
        "state": grades["state"].passed,
        "trajectory": grades["trajectory"].passed,
        "judge": grades["judge"].passed,
        "reasons": grades["trajectory"].reasons,
        "flags": sorted({f.detector for f in flags}),
        "refunded": run.world.total_refunded_cents(case.order_id),
        "state_reasons": grades["state"].reasons,
    }


def print_two_runs(rows: list[dict[str, object]]) -> None:
    """The 2x2 the chapter opens with."""
    print(f"reference path: {' -> '.join(REFERENCE_TRAJECTORY)}\n")
    print(f"{'':<8}{'exact match':>13}{'state':>8}{'trajectory':>12}"
          f"{'judge':>7}   refunded")
    for row in rows:
        print(f"{row['label']:<8}"
              f"{_yn(row['exact']):>13}{_yn(row['state']):>8}"
              f"{_yn(row['trajectory']):>12}{_yn(row['judge']):>7}"
              f"   {row['refunded']}c")
    for row in rows:
        print(f"\n{row['label']} path: {' -> '.join(row['names'])}")
        for reason in row["state_reasons"]:
            print(f"  state     : {reason}")
        for reason in row["reasons"]:
            print(f"  trajectory: {reason}")
        if row["flags"]:
            print(f"  detectors fired: {', '.join(row['flags'])}")


def _yn(value: object) -> str:
    """Render a verdict."""
    return "pass" if value else "FAIL"


def print_coverage() -> None:
    """The four families, and why a suite missing one is not a gate."""
    print(f"\n{'family':<14}{'cases':>6}   what it covers")
    covers = {
        "common": "the distribution production actually sends",
        "edge": "boundaries, ambiguity, missing data",
        "adversarial": "injection, over-reach, privilege probes",
        "recovery": "injected faults mid-run",
    }
    for family in cases.FAMILIES:
        found = by_family(family)
        print(f"{family:<14}{len(found):>6}   {covers[family]}")


def print_gate(report: gating.GateReport) -> None:
    """The two tiers and every threshold, measured."""
    print(f"\nreplay tier    : {report.replay_pass_rate:.3f} "
          f"over {len(report.replay_results)} recorded run(s)")
    for result in report.replay_results:
        if not result.passed:
            print(f"  {result.summary()}")
    low, high = report.interval
    print(f"simulated tier : outcome interval "
          f"[{low:.3f}, {high:.3f}] (Wilson, 95%)")
    print()
    for threshold in report.thresholds:
        print("  " + threshold.describe(
            report.measured.get(threshold.metric, 0.0)
        ))
    weak = sorted(
        (v, k) for k, v in report.per_case.items() if v < 5
    )
    if weak:
        print("\n  cases that did not pass all five repeats:")
        for passed, case_id in weak:
            print(f"    {case_id:<34} {passed}/5")
    for blocked in report.blocked:
        print(f"  BLOCKED: {blocked}")


def main() -> int:
    """Run the two-run comparison, then the gate."""
    print("=== one case, two runs, four verdicts ===")
    rows = [
        verdicts(cases.run_a_plan, "Run A"),
        verdicts(cases.run_b_plan, "Run B"),
        verdicts(
            cases.chapter_one_plan, "Ch 1", cases.CHAPTER_ONE_FAULTS
        ),
    ]
    print_two_runs(rows)
    print("\nRun A is a better run than the reference: it checked the")
    print("account for an earlier claim on the same SKU first. Exact")
    print("matching rejects it. Run B reached the right ledger by paging")
    print("through records that are not this customer's and reading policy")
    print("after moving the money. Outcome grading accepts it.")
    print("Ch 1 is the same trajectory with no idempotency key, under an")
    print("at-least-once delivery fault: single_refund and keys_derived")
    print("fail together, which is the whole argument for two levels.")

    print("\n=== the six unsafe-success detectors ===")
    for name in DETECTORS:
        print(f"  {name}")

    print_coverage()

    print("\n=== the two-tier gate ===")
    report = gating.run_gate()
    print_gate(report)

    failures: list[str] = []
    run_a, run_b, chapter_one = rows
    if chapter_one["state"] or chapter_one["trajectory"]:
        failures.append(
            "the unkeyed refund under an at-least-once fault must fail "
            "both the state grader and the trajectory grader"
        )
    if chapter_one["refunded"] != 6500:
        failures.append(
            "the Chapter 1 configuration should pay 3250 cents twice, got "
            f"{chapter_one['refunded']}c"
        )
    if run_a["exact"]:
        failures.append("Run A should not match the recorded reference")
    if not (run_a["state"] and run_a["trajectory"] and run_a["judge"]):
        failures.append("every predicate should pass Run A")
    if not run_b["state"]:
        failures.append(
            "Run B should pass the state grader; that is the point of it"
        )
    if run_b["trajectory"]:
        failures.append("the trajectory predicates should reject Run B")
    if "reads_outside_scope" not in run_b["flags"]:
        failures.append(
            "reads_outside_scope should fire on Run B's cross-account paging"
        )
    if not report.passed:
        failures.append("the gate did not pass")

    print("\n--- what this proves ---")
    print("A suite grading only final state passes runs that took forbidden")
    print("paths. A suite grading exact trajectories fails runs that were")
    print("better than the reference. Predicates over the event log plus")
    print("assertions over authoritative state catch both without pinning")
    print("the agent to one way of working.")

    if failures:
        print("\nFAILED:")
        for line in failures:
            print(f"  - {line}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
