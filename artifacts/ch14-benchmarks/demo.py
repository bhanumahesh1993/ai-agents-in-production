"""Run the Northstar set and print both numbers side by side.

    python artifacts/ch14-benchmarks/demo.py
    python artifacts/ch14-benchmarks/demo.py --holdout
    python artifacts/ch14-benchmarks/demo.py --contamination-check

The forty-task set, five attempts per task, against the seeded flaky model.
Prints the headline number a leaderboard would publish, the ``pass^5`` number
that should gate a release, the cost and latency percentiles from the same
runs, the per-task breakdown, and the split between tasks the agent finishes
alone and tasks that need the customer to act. Exits non-zero if ``pass^5``
on the critical refund scenarios falls below the configured floor.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import report as reporting
from contamination import check_tasks
from task import dual_control, holdout, load_tasks, solo, train

K = 5
SEED = reporting.SEED

#: The release floor. Workload-specific; copying another team's number is
#: meaningless. This one is set where the shipped set actually lands, so the
#: demo fails on a regression rather than on its first run.
CRITICAL_PASS_POW_K_FLOOR = 0.55

#: Tasks whose failure moves money or lets money move. These are the ones the
#: floor is enforced on; the read-only questions are not a release risk.
CRITICAL_PREFIXES = ("nr-lamp", "nr-mug", "nr-threshold", "nr-fullorder",
                     "nr-fraud", "nr-changedmind")


def print_task_table(reports: list[reporting.TaskReport]) -> None:
    """One row per task: attempts, pass@1, pass^k, mean cost."""
    print(f"{'task':<26}{'dc':>4}{'n':>4}{'pass@1':>9}{'pass^5':>9}"
          f"{'cents':>8}{'turns':>7}")
    for r in reports:
        cost = sum(a.cost_cents for a in r.attempts) / len(r.attempts)
        turns = sum(a.turns for a in r.attempts) / len(r.attempts)
        print(f"{r.task_id:<26}{'yes' if r.dual_control else '-':>4}"
              f"{len(r.attempts):>4}{r.pass_1:>9.3f}"
              f"{r.pass_pow_k(K):>9.3f}{cost:>8.1f}{turns:>7.1f}")


def print_headline(summary: dict[str, float], latencies: list[float]) -> None:
    """The two numbers, and the decision inputs beside them."""
    print(f"\n  headline_pass_at_1 : {summary['headline_pass_at_1']:.3f}   "
          "<- one attempt per task, what a leaderboard publishes")
    print(f"  pass_pow_k (k={K})   : {summary['pass_pow_k']:.3f}   "
          "<- all five attempts passed, what should gate a release")
    print(f"  p95_cost_cents     : {summary['p95_cost_cents']:.1f}")
    print(f"  p95_latency_ms     : "
          f"{reporting.percentile(latencies, 95):.1f}")


def run(tasks_label: str, tasks: list) -> int:
    """Measure one task set and assert the release floor on the money cases."""
    print(f"=== {tasks_label}: {len(tasks)} tasks x {K} attempts, "
          f"seed {SEED} ===")
    reports = reporting.run_suite(tasks, k=K, seed=SEED)
    print_task_table(reports)

    summary = reporting.summarise(reports, k=K)
    latencies = [a.latency_ms for r in reports for a in r.attempts]
    print_headline(summary, latencies)

    print("\n=== the same runs, split by who has to act ===")
    split = reporting.breakdown(reports, tasks)
    print(f"{'slice':<16}{'tasks':>6}{'pass@1':>9}{'pass^5':>9}")
    for label in ("solo", "dual_control"):
        row = split[label]
        print(f"{label:<16}{row['tasks']:>6.0f}{row['pass_1']:>9.3f}"
              f"{row['pass_pow_k']:>9.3f}")
    print("The agent is the same agent on both rows. The second row is work")
    print("the world does not change unless a person does something, and it")
    print("is where the headline number and the release number separate.")

    flagged = reporting.unsafe_successes(reports)
    print(f"\nforbidden actions taken : {len(flagged)} "
          "(counted separately from failures, on purpose)")

    critical = [
        r for r in reports
        if r.task_id.startswith(CRITICAL_PREFIXES)
    ]
    critical_pass_pow_k = (
        sum(r.all_passed for r in critical) / len(critical)
        if critical else 0.0
    )
    print(f"critical pass^{K}         : {critical_pass_pow_k:.3f} "
          f"(floor {CRITICAL_PASS_POW_K_FLOOR:.2f}, "
          f"{len(critical)} money-moving tasks)")

    failures: list[str] = []
    if critical_pass_pow_k < CRITICAL_PASS_POW_K_FLOOR:
        failures.append(
            f"critical pass^{K} of {critical_pass_pow_k:.3f} is below the "
            f"floor of {CRITICAL_PASS_POW_K_FLOOR:.2f}"
        )
    if flagged:
        failures.append(
            f"{len(flagged)} attempt(s) took a forbidden action"
        )
    if summary["pass_pow_k"] >= summary["headline_pass_at_1"]:
        failures.append(
            "pass^k should sit below the single-attempt headline: "
            f"{summary['pass_pow_k']:.3f} vs "
            f"{summary['headline_pass_at_1']:.3f}"
        )
    if split["dual_control"]["pass_1"] >= split["solo"]["pass_1"]:
        failures.append(
            "dual-control tasks should be harder than solo ones: "
            f"{split['dual_control']['pass_1']:.3f} vs "
            f"{split['solo']['pass_1']:.3f}"
        )
    return _verdict(failures)


def run_contamination_check() -> int:
    """Report tasks that share verbatim wording with public benchmarks."""
    tasks = load_tasks()
    print(f"=== contamination check over {len(tasks)} tasks ===")
    hits = check_tasks(tasks)
    if hits:
        for hit in hits:
            print(f"  {hit.describe()}")
    else:
        print("  no task shares a four-word window with the local corpus of")
        print("  public benchmark phrasings.")
    print("\nThis catches copy-paste and nothing else. A clean report is not")
    print("evidence that the set is private: check the retention and")
    print("training terms of the endpoint your harness actually calls.")
    return _verdict(
        [f"{len(hits)} task field(s) overlap public benchmark text"]
        if hits else []
    )


def _verdict(failures: list[str]) -> int:
    """Print any failures and return the process exit code."""
    if failures:
        print("\nFAILED:")
        for line in failures:
            print(f"  - {line}")
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    """Run the full set, the frozen holdout, or the contamination check."""
    args = list(sys.argv[1:] if argv is None else argv)
    if "--contamination-check" in args:
        return run_contamination_check()

    tasks = load_tasks()
    if "--holdout" in args:
        return run("frozen holdout split", holdout(tasks))

    code = run("Northstar internal benchmark", tasks)
    print(f"\nsplits: {len(train(tasks))} train, {len(holdout(tasks))} "
          f"holdout; {len(dual_control(tasks))} dual control, "
          f"{len(solo(tasks))} solo")
    print("\n--- what this proves ---")
    print("Both numbers come from the same runs on the same tasks. The gap")
    print("between them is not a bug in the agent: it is the information a")
    print("single-attempt benchmark discards, made visible on tasks you")
    print("chose, and attributable to a named slice of them.")
    return code


if __name__ == "__main__":
    sys.exit(main())
