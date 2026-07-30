"""Measure the Northstar critical set, then break the harness on purpose.

    python artifacts/ch13-reliability/demo.py

Prints the per-task table with pass@1, pass^k and a Wilson interval, the
bucketed view that decides a launch scope, and then re-runs the same suite
with one world shared across repetitions so you can watch a correct agent
score near zero. Exits non-zero if any of those properties does not hold.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import harness
import metrics
from tasks import CRITICAL_SET, by_bucket

N = 20          # repetitions per task
SEED = 1729
K = 4           # the pass^k column the report leads with
WEEKLY_TICKETS = 400
OBJECTIVE = 0.99


def print_table(report: harness.SuiteReport) -> None:
    """One row per task, then the suite line."""
    print(f"{'task':<30}{'n':>4}{'pass@1':>9}{'pass^2':>9}"
          f"{'pass^4':>9}   95% CI (pass@1)")
    for r in report.reports:
        low, high = metrics.wilson(r.successes, r.n)
        print(f"{r.task:<30}{r.n:>4}{r.pass_1:>9.3f}"
              f"{r.pass_k_values.get(2, 0.0):>9.3f}"
              f"{r.pass_k_values.get(4, 0.0):>9.3f}"
              f"   {low * 100:>5.1f}% - {high * 100:>5.1f}%")
    n_total = sum(r.n for r in report.reports)
    b_low, b_high = report.bootstrap_interval
    print(f"{'suite (' + str(len(report.reports)) + ' tasks)':<30}"
          f"{n_total:>4}{report.pass_1:>9.3f}{'':>9}"
          f"{report.pass_k:>9.3f}"
          f"   {b_low * 100:>5.1f}% - {b_high * 100:>5.1f}%  (bootstrap)")


def print_buckets(n: int, seed: int) -> dict[str, harness.SuiteReport]:
    """Measure each duration bucket separately and print the comparison."""
    out: dict[str, harness.SuiteReport] = {}
    print(f"\n{'bucket':<12}{'tasks':>6}{'pass@1':>9}{'pass^4':>9}")
    for bucket in ("short", "long"):
        tasks = by_bucket(bucket)
        rep = harness.run_suite(tasks, n=n, seed=seed, k=K)
        out[bucket] = rep
        print(f"{bucket:<12}{len(tasks):>6}{rep.pass_1:>9.3f}"
              f"{rep.pass_k:>9.3f}")
    return out


def print_error_budget(rate: float, label: str) -> float:
    """Convert a measured rate and a weekly volume into expected failures."""
    expected = WEEKLY_TICKETS * (1.0 - rate)
    budget = WEEKLY_TICKETS * (1.0 - OBJECTIVE)
    verdict = "inside budget" if expected <= budget else "over budget"
    print(f"  {label:<26} rate {rate:.3f} -> "
          f"{expected:5.1f} expected failures/week "
          f"(budget {budget:.0f}): {verdict}")
    return expected


def main() -> int:
    print("=== Northstar critical set, measured ===")
    print(f"{len(CRITICAL_SET)} tasks x {N} runs, seed {SEED}, "
          f"seeded FlakyModel over a scripted FakeModel\n")
    report = harness.run_suite(CRITICAL_SET, n=N, seed=SEED, k=K)
    print_table(report)

    print("\n=== the same numbers, bucketed by duration ===")
    print("A suite mean built from a slice that works and a slice that does")
    print("not describes neither. The buckets pick the launch scope.")
    buckets = print_buckets(N, SEED)

    print(f"\n=== error budget at a {OBJECTIVE:.0%} objective, "
          f"{WEEKLY_TICKETS} tickets/week ===")
    print_error_budget(report.pass_1, "whole suite")
    print_error_budget(buckets["short"].pass_1, "short bucket only")

    print("\n=== pass@k is not pass^k ===")
    sample = report.reports[0]
    print(f"  {sample.task}: over {sample.n} runs, "
          f"pass@4={metrics.pass_at_k(sample.results, 4):.3f} "
          f"vs pass^4={metrics.pass_k(sample.results, 4):.3f}")
    print("  Same runs. The first assumes a human discards the failures.")

    print("\n=== the harness bug: one world shared across repetitions ===")
    broken = harness.run_shared_world_suite(CRITICAL_SET, n=N, seed=SEED, k=K)
    print_table(broken)
    print("Nothing about the agent changed. Only the fixtures leaked.")

    failures: list[str] = []
    if not 0.0 < report.pass_1 < 1.0:
        failures.append(
            f"suite pass@1 is {report.pass_1:.3f}; the seeded wrapper should "
            "produce partial success, not a degenerate suite"
        )
    if report.pass_k >= report.pass_1:
        failures.append(
            f"pass^{K} ({report.pass_k:.3f}) should be below pass@1 "
            f"({report.pass_1:.3f})"
        )
    if buckets["long"].pass_1 >= buckets["short"].pass_1:
        failures.append(
            "the long bucket should be less reliable than the short one: "
            f"{buckets['long'].pass_1:.3f} vs {buckets['short'].pass_1:.3f}"
        )
    if broken.pass_1 >= report.pass_1:
        failures.append(
            "the shared-world harness should score lower than the correct "
            f"one: {broken.pass_1:.3f} vs {report.pass_1:.3f}"
        )

    print("\n--- what this proves ---")
    print("Every figure above is computed from the runs, not asserted, and")
    print("the interval is what a launch review should be reading. The last")
    print("table is a correct agent measured by a broken harness.")

    if failures:
        print("\nFAILED:")
        for line in failures:
            print(f"  - {line}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
