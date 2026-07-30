"""One task as a supervisor and as a swarm, with the refund timeout injected.

    python artifacts/ch06-topologies/demo.py
    python artifacts/ch06-topologies/demo.py --assert-single-refund

Three configurations against the same world fixture, the same deterministic
model, and the same tools, so the only variable is the topology. The demo
prints the per-run turn counts, the token totals, each handoff payload field
by field, and which component was holding the write when the timeout came
back.

The third configuration is the same swarm with the same agents, differing
only in whether the handoff carried its provenance. It produces a duplicate
refund against the ledger while reporting ``succeeded``.

The default exit code is 0 when all three behave as designed — including
that deliberate duplicate — so ``make demos`` stays a smoke test. Pass
``--assert-single-refund`` to assert the ledger holds exactly one refund
across every configuration; the third fails it on purpose.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import topology
from compare import CONFIGURATIONS, TraceRow, compare, print_handoff, print_table
from handoff import CONTRACT_CATEGORIES, Handoff, load_contract

#: What each configuration must do to the world. The first two settle the
#: claim once; the third is the failure this chapter is about.
EXPECTED_ROWS = {
    CONFIGURATIONS[0]: 1,
    CONFIGURATIONS[1]: 1,
    CONFIGURATIONS[2]: 2,
}


def check_contract_covers_every_category() -> list[str]:
    """The typed contract and the printed one must carry the same fields."""
    problems: list[str] = []
    typed = set(Handoff.__dataclass_fields__)
    for category, fields in CONTRACT_CATEGORIES.items():
        missing = [f for f in fields if f not in typed]
        if missing:
            problems.append(
                f"handoff contract category {category!r} is missing "
                f"{missing} from the Handoff dataclass"
            )

    printed = load_contract(Path(__file__).resolve().parent / "handoff.yaml")
    for required in (
        "origin_run_id",
        "origin_step_id",
        "goal",
        "allowed_actions",
        "prohibited_actions",
        "approval_threshold_cents",
        "budget_remaining",
        "trace_parent",
        "auth_context_ref",
        "return_to",
        "return_schema",
        "on_timeout",
    ):
        if required not in printed:
            problems.append(f"handoff.yaml is missing {required!r}")
    if "credential" in " ".join(printed.values()).lower():
        problems.append("handoff.yaml appears to carry a raw credential")
    return problems


def check_rows(rows: list[TraceRow]) -> list[str]:
    """Every claim the table makes, kept under test."""
    problems: list[str] = []
    by_name = {r.name: r for r in rows}

    for name, expected in EXPECTED_ROWS.items():
        row = by_name.get(name)
        if row is None:
            problems.append(f"configuration {name!r} did not run")
            continue
        if row.refund_rows != expected:
            problems.append(
                f"{name}: expected {expected} refund row(s), got "
                f"{row.refund_rows}"
            )
        if row.refunded_cents != expected * topology.REFUND_CENTS:
            problems.append(
                f"{name}: ledger holds {row.refunded_cents}c against a "
                f"{topology.REFUND_CENTS}c claim"
            )
        if row.status != "succeeded":
            problems.append(f"{name}: reported {row.status}, not succeeded")
        if row.owner_step < 0:
            problems.append(
                f"{name}: no component was recorded holding the write when "
                f"the timeout came back"
            )
        if row.approvals < 1:
            problems.append(
                f"{name}: the refund is over threshold on a flagged order "
                f"and should have asked a human"
            )

    carried = by_name[CONFIGURATIONS[1]]
    dropped = by_name[CONFIGURATIONS[2]]
    if carried.distinct_keys != 1:
        problems.append(
            f"the carried contract presented {carried.distinct_keys} "
            f"distinct keys; an anchored key is one identity per intent"
        )
    if dropped.distinct_keys < 2:
        problems.append(
            "the dropped contract presented one key; without the origin "
            "anchor a retried step must present a new identity"
        )
    if carried.turns_by_component == {} or dropped.turns_by_component == {}:
        problems.append("no per-component turn counts were recorded")
    return problems


def main(argv: list[str]) -> int:
    strict = "--assert-single-refund" in argv

    print("=== one task, three topologies ===")
    print(
        f"order {topology.ORDER_ID}, refund {topology.REFUND_CENTS} cents, "
        f"flagged fraud_review, above the 5000-cent threshold"
    )
    print("issue_refund is faulted: the write lands, the response is lost.\n")

    rows = compare()
    print_table(rows)

    print("\n=== turns, by component ===")
    for row in rows:
        parts = ", ".join(
            f"{k}={v}" for k, v in sorted(row.turns_by_component.items())
        )
        print(f"  {row.name:<24} {parts}")

    print("\n=== the handoff payload, field by field ===")
    for row in rows:
        print_handoff(row)

    print("\n=== keys presented to the refund service ===")
    for row in rows:
        shortened = [k[:12] + "..." for k in row.keys_presented]
        print(
            f"  {row.name:<24} {row.distinct_keys} distinct: {shortened}"
        )

    print("\n--- what this proves ---")
    print("Topology choice trades turns against tokens per turn in ways that")
    print("are measurable on a fixed script: the swarm finishes in fewer")
    print("turns because no subtask round-trips through a coordinator, and")
    print("each of its turns costs more because the accumulated transcript")
    print("travels with the transfer.")
    print("The safety difference between the two multi-agent designs is not")
    print("in the arrangement of the agents. It is in whether the handoff")
    print("carried origin_run_id and origin_step_id, which is the only")
    print("reason the second row settles the claim once and the third pays")
    print("it twice while reporting the same status.")

    problems = check_contract_covers_every_category() + check_rows(rows)
    if problems:
        print("\nFAILED:")
        for line in problems:
            print(f"  - {line}")
        return 1

    if strict:
        offenders = [r for r in rows if r.refund_rows != 1]
        print("\n--assert-single-refund: asserting on the ledger")
        for row in offenders:
            print(
                f"  - {row.name}: {row.refund_rows} refund rows, "
                f"{row.refunded_cents}c against a "
                f"{topology.REFUND_CENTS}c claim"
            )
        if offenders:
            print("\nFAILED: the ledger holds a duplicate refund.")
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
