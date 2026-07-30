"""Measure the Northstar critical set, then break the harness on purpose.

    python artifacts/ch13-reliability/demo.py
    python artifacts/ch13-reliability/demo.py --compare

Prints the per-task table with pass@1, pass^k and a Wilson interval, the
bucketed view that decides a launch scope, the error budget both buckets
imply, and then re-runs the same suite with one world shared across
repetitions so you can watch a correct agent score near zero. ``--compare``
runs the paired McNemar and bootstrap comparison of two agent versions
instead. Exits non-zero if any of those properties does not hold.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import harness
import metrics
from compare import compare_versions
from error_budget import BudgetLine, ErrorBudget
from tasks import CRITICAL_SET, by_bucket

N = 20          # repetitions per task
SEED = 1729
K = 4           # the pass^k column the report leads with
WEEKLY_TICKETS = 400
SHORT_SHARE = 0.6       # of the weekly queue that is short-bucket work
OBJECTIVE = 0.99        # what the whole queue is held to
PARTIAL_OBJECTIVE = 0.95    # what a first, narrower launch is held to


def print_table(report: harness.SuiteReport) -> None:
    """One row per task, then the suite line."""
    print(f"{'task':<26}{'n':>4}{'pass@1':>9}{'pass^2':>9}"
          f"{'pass^4':>9}   95% CI (pass@1)")
    for r in report.reports:
        low, high = metrics.wilson(r.successes, r.n)
        print(f"{r.task:<26}{r.n:>4}{r.pass_1:>9.3f}"
              f"{r.pass_k_values.get(2, 0.0):>9.3f}"
              f"{r.pass_k_values.get(4, 0.0):>9.3f}"
              f"   {low * 100:>5.1f}% - {high * 100:>5.1f}%")
    n_total = sum(r.n for r in report.reports)
    b_low, b_high = report.bootstrap_interval
    label = f"suite ({len(report.reports)} tasks)"
    print(f"{label:<26}{n_total:>4}{report.pass_1:>9.3f}{'':>9}"
          f"{report.pass_k:>9.3f}"
          f"   {b_low * 100:>5.1f}% - {b_high * 100:>5.1f}%  (bootstrap)")


def print_buckets(n: int, seed: int) -> dict[str, harness.SuiteReport]:
    """Measure each duration bucket separately and print the comparison."""
    out: dict[str, harness.SuiteReport] = {}
    print(f"\n{'bucket':<12}{'tasks':>6}{'turns':>8}{'pass@1':>9}"
          f"{'pass^4':>9}")
    for bucket in ("short", "long"):
        tasks = by_bucket(bucket)
        rep = harness.run_suite(tasks, n=n, seed=seed, k=K)
        out[bucket] = rep
        turns = max(t.plan.turns for t in tasks)
        print(f"{bucket:<12}{len(tasks):>6}{turns:>8}{rep.pass_1:>9.3f}"
              f"{rep.pass_k:>9.3f}")
    return out


def print_budget_line(line: BudgetLine) -> None:
    """One error-budget row."""
    print(f"  {line.label:<24} rate {line.rate:.3f} over "
          f"{line.volume:>3} tickets -> {line.expected_failures:5.1f} "
          f"failures/week (budget {line.budget_failures:.0f}): "
          f"{line.verdict()}")


def report_suite() -> int:
    """The default run: measure, bucket, budget, then break the harness."""
    print("=== Northstar critical set, measured ===")
    print(f"{len(CRITICAL_SET)} tasks x {N} runs, seed {SEED}, "
          f"version {harness.BASELINE.name}, seeded FlakyModel over a "
          f"planned FakeModel\n")
    report = harness.run_suite(CRITICAL_SET, n=N, seed=SEED, k=K)
    print_table(report)

    print("\n=== the same numbers, bucketed by duration ===")
    print("A suite mean built from a slice that works and a slice that does")
    print("not describes neither. The buckets pick the launch scope.")
    buckets = print_buckets(N, SEED)

    print("\n=== error budget ===")
    short_volume = int(WEEKLY_TICKETS * SHORT_SHARE)
    strict = ErrorBudget(OBJECTIVE, WEEKLY_TICKETS)
    print(f"at a {OBJECTIVE:.0%} objective over "
          f"{WEEKLY_TICKETS} tickets/week:")
    whole_line = strict.assess("whole suite", report.pass_1)
    short_strict = strict.scaled_to(short_volume).assess(
        "short bucket only", buckets["short"].pass_1
    )
    print_budget_line(whole_line)
    print_budget_line(short_strict)

    relaxed = ErrorBudget(PARTIAL_OBJECTIVE, short_volume)
    print(f"at a {PARTIAL_OBJECTIVE:.0%} objective over the "
          f"{short_volume} short tickets/week:")
    short_relaxed = relaxed.assess(
        "short bucket only", buckets["short"].pass_1
    )
    print_budget_line(short_relaxed)
    print("  The agent is the same agent on all three lines, and the same")
    print("  measured rate. What moved is the slice of the queue it is")
    print("  asked to carry and the objective that slice is held to, which")
    print("  is exactly the decision the measurement exists to inform.")

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
    if whole_line.inside_budget:
        failures.append(
            f"a suite at {report.pass_1:.3f} cannot fit a "
            f"{OBJECTIVE:.0%} objective over {WEEKLY_TICKETS} tickets"
        )
    if not short_relaxed.inside_budget:
        failures.append(
            "the short bucket should fit the narrower launch: "
            f"{short_relaxed.expected_failures:.1f} expected against a "
            f"budget of {short_relaxed.budget_failures:.1f}"
        )

    print("\n--- what this proves ---")
    print("Every figure above is computed from the runs, not asserted, and")
    print("the interval is what a launch review should be reading. The last")
    print("table is a correct agent measured by a broken harness.")
    return _verdict(failures)


def report_comparison() -> int:
    """``--compare``: two versions, same tasks, same seeds, paired."""
    print("=== paired comparison, same tasks and same seeds ===")
    result = compare_versions(CRITICAL_SET, n=N, seed=SEED, k=K)

    print(f"{'version':<18}{'runs':>6}{'pass@1':>9}{'pass^4':>9}")
    for suite in (result.baseline, result.candidate):
        runs = sum(r.n for r in suite.reports)
        print(f"{suite.version.name:<18}{runs:>6}{suite.pass_1:>9.3f}"
              f"{suite.pass_k:>9.3f}")

    low, high = result.delta_interval
    print(f"\nconcordant pairs   : {result.concordant} "
          "(carry no information about which is better)")
    print(f"discordant pairs   : {result.discordant} "
          f"({result.candidate_wins} to the candidate, "
          f"{result.baseline_wins} to the baseline)")
    print(f"McNemar exact p    : {result.p_value:.4f}")
    print(f"effect size        : {result.delta:+.3f} pass@1, bootstrap over "
          f"tasks [{low:+.3f}, {high:+.3f}]")
    print(f"reading            : "
          f"{'a real difference' if result.significant else 'not resolved'} "
          "at the 5% level")

    print("\n--- what this proves ---")
    print("The p value comes from the discordant pairs alone, which is why")
    print("this resolves on a fraction of the runs an unpaired comparison")
    print("would need. The effect size is reported beside it, because")
    print("significance is not a claim that the change was worth making.")

    failures: list[str] = []
    if result.concordant + result.discordant != sum(
        r.n for r in result.baseline.reports
    ):
        failures.append("pairs do not account for every run")
    if result.discordant == 0:
        failures.append(
            "no discordant pairs: the two versions were indistinguishable, "
            "so this demo is not demonstrating a comparison"
        )
    if result.delta <= 0:
        failures.append(
            f"the candidate should beat the baseline, got {result.delta:+.3f}"
        )
    return _verdict(failures)


def _verdict(failures: list[str]) -> int:
    """Print any failures and return the process exit code."""
    if failures:
        print("\nFAILED:")
        for line in failures:
            print(f"  - {line}")
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    """Run the report, or the paired comparison with ``--compare``."""
    args = list(sys.argv[1:] if argv is None else argv)
    if "--compare" in args:
        return report_comparison()
    return report_suite()


if __name__ == "__main__":
    sys.exit(main())
