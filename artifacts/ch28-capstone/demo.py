"""The capstone: the whole Northstar system, end to end, on a laptop.

    python artifacts/ch28-capstone/demo.py
    python artifacts/ch28-capstone/demo.py --grade
    python artifacts/ch28-capstone/demo.py --grade --drift 0.18

Four cases, composed from the shared packages and nothing else:

1. **A damaged-item ticket that resolves automatically.** Under the
   5,000-cent approval threshold, so no human is asked. One refund, one
   message, a complete trace.
2. **A high-value ticket that suspends for approval and resumes on a
   fingerprint match.** The approval binds the sha256 of the exact call.
   Change the amount by one cent and it no longer applies.
3. **A fraud case that hands off to the specialist.** The run was admitted
   without ``refunds:write`` at all, so the refund it was asked for is
   not merely gated — it is unauthorised, and the policy decision point
   says so outside the model.
4. **A worker killed mid-refund that resumes without double-paying.** The
   journal recorded the effect before the step was done, so the replay
   walks back to the crash without re-executing anything. The same case
   run without a derived idempotency key, against a refund service that
   times out after committing, pays twice.

``--grade`` runs the same four cases repeatedly against a drifting model
and reports ``pass^k`` with confidence intervals, action integrity, trace
completeness, and a computed GO / NO-GO. That report is the artifact to put
in front of a go-live review.

Exits non-zero if any of the four properties fails, or if ``--grade``
returns NO-GO at the default drift.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import argparse  # noqa: E402

from capstone import Capstone, CaseResult  # noqa: E402
from gate import DEFAULT_DRIFT, grade_suite, render  # noqa: E402
from northstar_contracts import ToolCall  # noqa: E402
from northstar_evals import trajectory  # noqa: E402
from northstar_policy import Decision  # noqa: E402
from scenarios import (  # noqa: E402
    CASES,
    CRASH_RECOVERY,
    FRAUD_ORDER,
    FULL_ORDER_CENTS,
    LAMP_ORDER,
    LAMP_SHADE_CENTS,
    Case,
)

WIDTH = 74


def run_case(case: Case, **overrides: object) -> tuple[Capstone, CaseResult]:
    """Handle one case on a fresh system and return both."""
    system = Capstone(**overrides)  # type: ignore[arg-type]
    result = system.handle(
        case.ticket,
        list(case.script),
        list(case.graders),
        crash_after_step=case.crash_after_step,
        approve_by=case.approve_by,
        fault=case.fault,
    )
    return system, result


def header(case: Case, result: CaseResult, suffix: str = "") -> None:
    """Print what admission decided and what the run did."""
    print("\n" + "=" * WIDTH)
    print(f"{case.ticket.ticket_id}  {case.headline}{suffix}")
    print("=" * WIDTH)
    print(f"  admitted as   : risk={result.admission.risk} "
          f"budget={result.admission.budget_cents}c "
          f"turns={result.admission.max_turns}")
    print(f"  config hash   : {result.admission.short_config_hash}")
    print(f"  scopes        : "
          f"{', '.join(sorted(result.admission.principal.scopes))}")
    print(f"  trajectory    : {' -> '.join(trajectory(result.state)) or '-'}")
    print(f"  run status    : {result.state.status}")
    print(f"  ledger        : {result.mutations} side effect(s), "
          f"{result.refunds} refund(s)")
    print(f"  evidence      : {len(result.journal)} journal record(s), "
          f"{len(result.spans)} span(s), "
          f"{result.cost_cents}c (ILLUSTRATIVE)")
    print(f"  graded        : {'PASS' if result.passed else 'FAIL'}")
    for reason in result.grade.reasons:
        print(f"    - {reason}")
    for note in result.notes:
        print(f"  note          : {note}")


def case_damaged_item(failures: list[str]) -> None:
    """A routine ticket, resolved without asking anyone."""
    case = CASES[0]
    _system, result = run_case(case)
    header(case, result)
    print("  no human was asked: the refund is below the 5,000-cent")
    print("  threshold, and admission bounded the run before it started.")

    world = result.world
    if world.total_refunded_cents(LAMP_ORDER) != LAMP_SHADE_CENTS:
        failures.append("damaged_item: wrong amount refunded")
    if result.refunds != 1 or len(world.messages) != 1:
        failures.append("damaged_item: wrong number of side effects")
    if result.approvals:
        failures.append("damaged_item: an approval was requested")
    if not result.passed:
        failures.append("damaged_item: the graders rejected the run")


def case_high_value(failures: list[str]) -> None:
    """A ticket a human has to decide, and an approval that binds."""
    case = CASES[1]
    system = Capstone()
    result = system.handle(
        case.ticket, list(case.script), list(case.graders)
    )
    print("\n" + "=" * WIDTH)
    print(f"{case.ticket.ticket_id}  {case.headline}")
    print("=" * WIDTH)
    print(f"  run status    : {result.state.status}")
    print("  approval inbox (the payload, not a paraphrase):")
    for item in system.inbox():
        print(f"    request     : {item['id']}  {item['tool']}")
        print(f"    arguments   : {item['arguments']}")
        print(f"    reason      : {item['reason']}")
        print(f"    fingerprint : {item['fingerprint'][:24]}...")

    if result.state.status != "waiting_approval":
        failures.append("high_value: the run did not suspend for a human")
    if len(system.approvals.pending()) != 1:
        failures.append("high_value: no approval was requested")

    system2, approved = run_case(case)
    header(case, approved, suffix="\n           after the approval was decided")

    exact = ToolCall(
        "c3",
        "issue_refund",
        {
            "order_id": LAMP_ORDER,
            "amount_cents": FULL_ORDER_CENTS,
            "reason": "damaged",
        },
    )
    one_cent_less = ToolCall(
        "c3",
        "issue_refund",
        {
            "order_id": LAMP_ORDER,
            "amount_cents": FULL_ORDER_CENTS - 1,
            "reason": "damaged",
        },
    )
    run_id = approved.admission.run_id
    binds = system2.would_be_approved(exact, run_id)
    leaks = system2.would_be_approved(one_cent_less, run_id)
    print(f"  approval binds the exact call        : {binds}")
    print(f"  ... and the same call one cent lower : {leaks}")

    if approved.world.total_refunded_cents(LAMP_ORDER) != FULL_ORDER_CENTS:
        failures.append("high_value: the approved refund did not land")
    if approved.refunds != 1:
        failures.append("high_value: the resume double-paid")
    if not binds or leaks:
        failures.append(
            "high_value: the approval is not bound to the exact call"
        )
    if not approved.passed:
        failures.append("high_value: the graders rejected the run")


def case_fraud_handoff(failures: list[str]) -> None:
    """A case the agent must not decide, enforced outside the model."""
    case = CASES[2]
    system, result = run_case(case)
    header(case, result)

    refund = ToolCall(
        "cx",
        "issue_refund",
        {
            "order_id": FRAUD_ORDER,
            "amount_cents": 24000,
            "reason": "changed_mind",
        },
    )
    decision = system.policy_decision(result.admission.principal, refund)
    print(f"  had it tried to refund anyway        : {decision.value}")
    print("  the run holds no refunds:write scope, so the authority was")
    print("  never issued. Nothing the model reads can change that.")

    if result.world.total_refunded_cents(FRAUD_ORDER) != 0:
        failures.append("fraud_handoff: money moved on a flagged order")
    if not result.world.escalations:
        failures.append("fraud_handoff: no specialist case was opened")
    if decision is not Decision.DENY:
        failures.append(
            f"fraud_handoff: policy said {decision.value}, expected deny"
        )
    if not result.passed:
        failures.append("fraud_handoff: the graders rejected the run")


def case_crash_recovery(failures: list[str]) -> None:
    """A worker killed at the worst possible moment, and the same run
    without a key."""
    case = CRASH_RECOVERY
    _system, result = run_case(case)
    header(case, result)
    print(f"  effects replayed from the journal    : "
          f"{result.replayed_effects}")
    print(f"  effects executed after the resume    : "
          f"{result.executed_effects}")

    if not (result.crashed and result.resumed):
        failures.append("crash_recovery: the worker did not die and resume")
    if result.refunds != 1:
        failures.append(
            f"crash_recovery: {result.refunds} refund(s) after the resume"
        )
    if result.replayed_effects < 1:
        failures.append(
            "crash_recovery: the resume re-executed instead of replaying"
        )
    if not result.passed:
        failures.append("crash_recovery: the graders rejected the run")

    print("\n  the same ticket against a refund service that times out")
    print("  after committing, with and without a derived key:")
    for keyed in (True, False):
        system = Capstone(idempotency=keyed)
        outcome = system.handle(
            case.ticket,
            list(case.script),
            list(case.graders),
            fault="timeout",
        )
        label = "derived key" if keyed else "no key     "
        print(
            f"    {label} : {outcome.refunds} refund(s), "
            f"{outcome.world.total_refunded_cents(LAMP_ORDER)}c against a "
            f"{LAMP_SHADE_CENTS}c claim, status={outcome.state.status}"
        )
        expected = 1 if keyed else 2
        if outcome.refunds != expected:
            failures.append(
                f"crash_recovery: keyed={keyed} produced "
                f"{outcome.refunds} refund(s), expected {expected}"
            )


def show_grade(failures: list[str], *, n: int, drift: float) -> None:
    """The go-live report."""
    report = grade_suite(n=n, drift=drift)
    print()
    for line in render(report):
        print(line)
    print(
        "\nEvery figure above is computed from runs that happened: successes"
        "\nare a state grader's verdict on the authoritative world, pass^k"
        "\ncomes from the observed pass/fail vector, and the intervals are"
        "\nWilson intervals over the same counts."
    )

    # A gate that cannot block is a dashboard. Run the same suite against
    # a model that drifts four times as often and show it refusing.
    degraded = grade_suite(n=max(4, n // 2), drift=min(0.9, drift * 4))
    print(
        f"\nthe same suite at {min(0.9, drift * 4):.0%} drift: "
        f"{degraded.decision}"
    )
    for problem in degraded.blocking()[:3]:
        print(f"  blocked by: {problem}")

    if report.runs != n * len(CASES):
        failures.append("the graded suite did not run every case")
    if (report.decision == "GO") != (report.blocking() == []):
        failures.append("the decision does not follow from the targets")
    if degraded.decision != "NO-GO":
        failures.append(
            "the gate did not block a suite that should have failed"
        )
    if drift == DEFAULT_DRIFT and report.decision != "GO":
        failures.append(
            f"the suite is NO-GO at the default drift: "
            f"{'; '.join(report.blocking())}"
        )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--grade",
        action="store_true",
        help="report pass^k with confidence intervals and a GO decision",
    )
    parser.add_argument("--n", type=int, default=12)
    parser.add_argument("--drift", type=float, default=DEFAULT_DRIFT)
    args = parser.parse_args()

    failures: list[str] = []
    if args.grade:
        show_grade(failures, n=args.n, drift=args.drift)
    else:
        case_damaged_item(failures)
        case_high_value(failures)
        case_fraud_handoff(failures)
        case_crash_recovery(failures)

        print("\n" + "=" * WIDTH)
        print("four mechanisms, four layers, none of them a better model")
        print("=" * WIDTH)
        print("  typed tool contract : a key derived from the run and the")
        print("                        step, enforced by the target system")
        print("  policy and approval : outside the model, at the action")
        print("                        boundary, bound to a call fingerprint")
        print("  durable journal     : the effect recorded before the step")
        print("                        is done, so a replay resumes at the")
        print("                        first unrecorded one")
        print("  state grader        : reads the ledger, never the")
        print("                        transcript's account of itself")
        print("\n  run with --grade for the same four cases as pass^k.")

    if failures:
        print("\nFAILED:")
        for line in failures:
            print(f"  - {line}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
