"""Run six patterns on one task, twice, and print what each one cost.

    python artifacts/ch04-patterns/demo.py

First on the clean fixture, where every pattern produces the same four tool
calls and the same correct refund, so the only difference between them is
price. Then with ``World.inject_fault("issue_refund", kind="timeout")``,
which reproduces the Chapter 1 incident: the refund commits, the response is
lost, the agent retries without a key, and the ledger ends with two rows.

Exits non-zero if any pattern other than the state check claims to have
caught the duplicate, if the state check misses it, or if the clean fixture
does not verify for all six.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import task
from measure import PatternCost, measure_all, table

#: The only pattern that reads the system of record rather than the run's
#: account of itself. It is also the cheapest thing on the ladder.
STATE_CHECK = "State verification"


def show(label: str, costs: list[PatternCost]) -> None:
    """Print one table and any notes the patterns left behind."""
    print(f"\n=== {label} ===")
    table(costs)
    notes = [(c.name, n) for c in costs for n in c.notes]
    if notes:
        print()
        for name, note in notes:
            print(f"  {name}: {note}")


def check_clean(costs: list[PatternCost]) -> list[str]:
    """On a world with no fault, every pattern must land one refund."""
    failures = []
    for c in costs:
        if c.refund_rows != 1 or not c.verified:
            failures.append(
                f"{c.name}: clean fixture left {c.refund_rows} refund row(s), "
                f"verified={c.verified}"
            )
        if c.caught:
            failures.append(
                f"{c.name}: reported catching something on a clean fixture"
            )
    return failures


def check_faulted(costs: list[PatternCost]) -> list[str]:
    """The chapter's central claim, kept under test."""
    failures = []
    for c in costs:
        if c.refund_rows != 2:
            failures.append(
                f"{c.name}: expected the timeout to double-refund, got "
                f"{c.refund_rows} row(s)"
            )
        if c.verified:
            failures.append(f"{c.name}: the authoritative check should fail")
        if c.name == STATE_CHECK and not c.caught:
            failures.append(f"{c.name}: did not catch the duplicate")
        if c.name != STATE_CHECK and c.caught:
            failures.append(
                f"{c.name}: claims to have caught a duplicate it cannot see"
            )
    return failures


def main() -> int:
    print(f"task: {task.TASK}")
    print(
        f"order {task.ORDER_ID}, {task.AMOUNT_CENTS} cents, "
        f"reason {task.REASON}"
    )
    print("\nlatency in mock mode is not latency: 'Calls' is the number of")
    print("sequential model round trips, which is the part that does not")
    print("overlap. Multiply by your provider's per-call time.")

    clean = measure_all()
    show("clean fixture", clean)

    faulted = measure_all(fault=True)
    show("same six, with the refund timeout injected", faulted)

    print("\nstatus reported by each run, with the ledger holding two rows:")
    for c in faulted:
        print(f"  {c.name:<28} {c.status}")

    print("\n--- what this proves ---")
    print("Reasoning patterns trade tokens and round trips for capability on")
    print("the happy path. Under the fault, five of the six finish with a")
    print("transcript that reads as a clean recovery, the critic pass")
    print("included, because the message is accurate about everything it")
    print("mentions. The information that the money moved twice is not in")
    print("the transcript. It is in the ledger, and only the check that")
    print("reads the ledger finds it.")

    failures = check_clean(clean) + check_faulted(faulted)
    if failures:
        print("\nFAILED:")
        for line in failures:
            print(f"  - {line}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
