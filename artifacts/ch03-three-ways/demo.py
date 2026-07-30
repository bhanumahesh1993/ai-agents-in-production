"""Run one triage agent three ways and print the scorecard.

    python artifacts/ch03-three-ways/demo.py

All three runtimes see the same scripted model, the same tool contracts, and
the same world. Any difference in the table is the framework's. Exits
non-zero if any port produces a different tool-call trajectory, or leaves
more than one refund in the ledger after a replay or after a kill.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import shared.triage as triage
from scorecard import PORTS, PortScore, print_scorecard, score_all


def report_equivalence(scores: list[PortScore]) -> list[str]:
    """Print what all three ports agree on, and collect any disagreement."""
    print("\n=== the part the framework does not change ===")
    failures: list[str] = []
    for s in scores:
        print(f"\n{s.port}")
        print(f"  trajectory        : {' -> '.join(s.trajectory)}")
        print(f"  ledger            : {list(s.refunds)} cents")
        print(f"  after a replay    : {list(s.refunds_after_replay)} cents")
        print(f"  after a kill      : {list(s.refunds_after_kill)} cents")

        if s.trajectory != triage.EXPECTED_CALLS:
            failures.append(
                f"{s.port}: trajectory {list(s.trajectory)} is not "
                f"{list(triage.EXPECTED_CALLS)}"
            )
        for label, ledger in (
            ("first run", s.refunds),
            ("replay", s.refunds_after_replay),
            ("kill and resume", s.refunds_after_kill),
        ):
            if list(ledger) != [triage.AMOUNT_CENTS]:
                failures.append(
                    f"{s.port}: ledger after {label} is {list(ledger)}, "
                    f"expected [{triage.AMOUNT_CENTS}]"
                )
    return failures


def main() -> int:
    scores = score_all(PORTS)

    print("=== scorecard: one triage agent, three ways ===")
    print(f"order {triage.ORDER_ID}, {triage.AMOUNT_CENTS} cents, "
          f"reason {triage.REASON}\n")
    print_scorecard(scores)

    failures = report_equivalence(scores)

    print("\n--- what this proves ---")
    print("The framework changes how much glue you write, how finely the")
    print("run is checkpointed, whether your own policy check has a seat,")
    print("and what leaves the process by default.")
    print("It does not change the correctness guarantee. That lives in the")
    print("tool contract and in the refund service, which is why all three")
    print("ledgers read the same after a replay and after a kill.")

    if failures:
        print("\nFAILED:")
        for line in failures:
            print(f"  - {line}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
