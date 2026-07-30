"""Three compaction configurations on one twelve-task set.

    python artifacts/ch07-context/demo.py

Prints, for each configuration, pass@1 and pass^k, tokens per run,
compaction events, and the refund ledger. Exits non-zero if the pinned
configuration ever produces a duplicate refund, if the naive one never
does, or if no compaction ever fires -- because a demo in which nothing
compacted proves nothing about compaction.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from eval_compaction import (  # noqa: E402
    BUDGET,
    MODES,
    RunOutcome,
    budget_report,
    compare,
    summarise,
)
from northstar_evals import pass_k  # noqa: E402
from session import AMOUNT, ORDER  # noqa: E402

LABELS = {
    "none": "no compaction",
    "naive": "naive compaction (summary only)",
    "pinned": "pinned compaction (computed facts + summary)",
}


def report(mode: str, outcomes: list[RunOutcome]) -> dict[str, int]:
    """Print one configuration's table and return its rolled-up row."""
    row = summarise(outcomes)
    results = [o.passed for o in outcomes]
    print(f"\n=== {LABELS[mode]} ===")
    print(f"tasks passed     : {row['passed']}/{row['tasks']}")
    print(f"pass@1           : {row['passed'] / row['tasks']:.2f}")
    print(f"pass^4           : {pass_k(results, 4):.2f}")
    print(f"tokens per run   : {row['tokens_per_run']} "
          f"({row['tokens_per_turn']} per turn)")
    print(f"peak context     : {row['peak_tokens']} tokens "
          f"(ceiling {BUDGET.content_ceiling})")
    print(f"compaction events: {row['compactions']}")
    print(f"runs that failed : {row['errors']} by exception")
    print(f"duplicate refunds: {row['duplicate_refunds']} task(s)")

    worst = max(outcomes, key=lambda o: (o.refund_rows, o.task))
    print(f"ledger, task {worst.task:>2}   : {worst.refund_rows} refund(s), "
          f"{worst.refunded_cents} cents against a {AMOUNT}-cent claim")
    return row


def main() -> int:
    print(f"order {ORDER}, damaged lamp shade, {AMOUNT} cents")
    print(f"budget: {BUDGET}")

    results = compare()
    rows = {mode: report(mode, outcomes)
            for mode, outcomes in results.items()}

    print("\n--- per-line-item accounting, longest task ---")
    for mode, outcomes in results.items():
        print(f"{LABELS[mode]:<44} {budget_report(outcomes[-1])}")

    print("\n--- what this proves ---")
    print("Uncompacted runs pay for their history on every turn and the")
    print("longest ones exhaust the cost ceiling. Both compacted")
    print("configurations hold the same context ceiling at the same cost")
    print("per turn, and they summarise identically well. Only the refund")
    print("ledger separates them, and the only difference between them is")
    print("a block of text computed from the event log that never reaches")
    print("the summariser.")

    failures: list[str] = []
    if rows["pinned"]["duplicate_refunds"]:
        failures.append(
            f"pinned compaction produced a duplicate refund in "
            f"{rows['pinned']['duplicate_refunds']} task(s)"
        )
    if rows["pinned"]["passed"] != rows["pinned"]["tasks"]:
        failures.append(
            f"pinned compaction passed only "
            f"{rows['pinned']['passed']}/{rows['pinned']['tasks']} tasks"
        )
    if not rows["naive"]["duplicate_refunds"]:
        failures.append(
            "naive compaction produced no duplicate refund; the task set "
            "is too short to exercise the failure"
        )
    if not rows["naive"]["compactions"]:
        failures.append("nothing ever compacted; the budget is too loose")
    if not rows["none"]["errors"]:
        failures.append(
            "no uncompacted run exhausted its budget; the cost ceiling is "
            "too loose to show what compaction buys"
        )
    for mode in MODES:
        if mode != "none" and rows[mode]["peak_tokens"] > BUDGET.total:
            failures.append(
                f"{mode} compaction let the context reach "
                f"{rows[mode]['peak_tokens']} tokens, past the "
                f"{BUDGET.total}-token total"
            )

    if failures:
        print("\nFAILED:")
        for line in failures:
            print(f"  - {line}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
