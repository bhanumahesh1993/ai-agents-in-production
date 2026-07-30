"""The 24,000-cent refund, paused, and three ways it can end.

    python artifacts/ch20-approvals/demo.py
    python artifacts/ch20-approvals/demo.py --fail-on-tamper

Runs the refund to a pause, prints the rendered approval payload, then
drives three endings: approve and resume, approve then let the agent
re-plan the amount before resume, and let the approval expire. It then
shows an approver *correcting* the same call, a tool-version bump
invalidating a parked approval, the never-permitted action being refused by
the schema, and the containment ladder.

Exits non-zero if any of those behaves differently. With
``--fail-on-tamper`` the tampered ending is fatal on its own, which is the
form the chapter describes: an unbound fingerprint, and an empty ledger.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import tempfile

from classes import (
    ACTION_CLASSES,
    APPROVAL_THRESHOLD_CENTS,
    class_for,
    refund_to_non_payer,
)
from containment import ContainmentLog, Tripwire, friction_decreases, untested
from fingerprint import fingerprint
from northstar_policy import BudgetExceeded
from payload import render
from run import (
    AMOUNT,
    ORDER,
    PRINCIPAL,
    RUN_ID,
    TAMPERED_AMOUNT,
    TOOL_VERSION,
    refund_call,
    replan,
    start_run,
)

APPROVER = "rota:fraud-review"
CORRECTED = 8400


def clock_from(start: float = 1000.0):  # noqa: ANN201 - a tiny local factory
    """A clock a test or a demo can wind forward, so nothing sleeps."""
    now = [start]
    return now, (lambda: now[0])


def ending_approve(failures: list[str]) -> None:
    """Approve the exact call, resume, and land one refund."""
    print("\n=== ending 1: approve and resume ===")
    inbox_file = Path(tempfile.mkdtemp(prefix="ch20-inbox-")) / "inbox.jsonl"
    run = start_run(inbox_path=str(inbox_file))
    request = run.pending[0]

    print(f"run status     : {run.state.status}")
    print(f"inbox file     : {inbox_file}")
    print(f"notification   : {run.inbox.notification(request.id)}")
    print("\n-- the payload the approver opens --")
    print(render(run.inbox.payload_for(request.id)))

    run.inbox.approve(request.id, by=APPROVER, note="photos verified")
    state = run.resume()

    print(f"\nafter approval : {state.status}")
    print(f"ledger         : {run.refund_rows} row(s), "
          f"{run.refunded_cents} cents")
    print(f"inbox events   : {[e['event'] for e in run.inbox.events]}")
    print(f"file replays   : {len(run.inbox.replay_file())} record(s)")

    if state.status != "succeeded":
        failures.append(f"approved run ended {state.status!r}")
    if run.refunded_cents != AMOUNT or run.refund_rows != 1:
        failures.append(
            f"approved run left {run.refund_rows} row(s) and "
            f"{run.refunded_cents} cents"
        )
    if len(run.inbox.replay_file()) != len(run.inbox.events):
        failures.append("the file-backed inbox lost a record")


def ending_tamper(failures: list[str], fatal: bool) -> None:
    """Approve 24,000, then let the run ask for 240,000 on resume."""
    print("\n=== ending 2: approve, then the run re-plans the amount ===")
    run = start_run()
    request = run.pending[0]
    approved_fp = request.fingerprint
    run.inbox.approve(request.id, by=APPROVER)

    tampered_fp = fingerprint(
        refund_call(TAMPERED_AMOUNT), PRINCIPAL, RUN_ID, TOOL_VERSION
    )
    print(f"approved       : {AMOUNT} cents, fingerprint {approved_fp[:16]}")
    print(f"resumed with   : {TAMPERED_AMOUNT} cents, "
          f"fingerprint {tampered_fp[:16]}")
    bound_before = run.inbox.find(tampered_fp)
    print(f"decision found : {bound_before}")

    state = run.loop.resume(replan(run.state, TAMPERED_AMOUNT))
    print(f"after resume   : {state.status}")
    print(f"ledger         : {run.refund_rows} row(s), "
          f"{run.refunded_cents} cents")
    reopened = run.inbox.pending()
    print(f"re-requested   : {[p.id for p in reopened]}")
    if reopened:
        asked = reopened[-1].arguments["arguments"]["amount_cents"]
        print(f"the diff       : {AMOUNT} -> {asked}")

    if approved_fp == tampered_fp:
        failures.append("one changed integer produced the same fingerprint")
    if bound_before is not None:
        failures.append("a modified call found a prior decision")
    reopened_record = run.inbox.find(tampered_fp)
    if reopened_record is None or reopened_record.approved:
        failures.append(
            "the modified call did not produce a fresh, undecided request"
        )
    if run.refunded_cents != 0:
        failures.append(
            f"the tampered run moved {run.refunded_cents} cents"
        )
    if state.status != "waiting_approval":
        failures.append(
            f"the tampered run ended {state.status!r}, not waiting_approval"
        )
    if fatal:
        failures.append(
            f"--fail-on-tamper: unbound fingerprint {tampered_fp[:16]} for a "
            f"{TAMPERED_AMOUNT}-cent call; the ledger stays empty"
        )


def ending_expire(failures: list[str]) -> None:
    """Nobody answers. Expiry rejects, and escalation never widens."""
    print("\n=== ending 3: nobody answers ===")
    now, clock = clock_from()
    run = start_run(clock=clock)
    request = run.pending[0]

    now[0] += 5 * 3600
    at_five = run.inbox.escalate(request.id)
    now[0] += 8 * 3600
    at_thirteen = run.inbox.escalate(request.id)
    print(f"after 5h       : escalated to {at_five}")
    print(f"after 13h      : {at_thirteen}")
    print(f"status         : {run.inbox.status(refund_call(), RUN_ID)}")

    state = run.loop.resume(run.state)
    print(f"after resume   : {state.status}")
    print(f"ledger         : {run.refund_rows} row(s), "
          f"{run.refunded_cents} cents")

    if at_five != APPROVER or at_thirteen != "reject":
        failures.append(
            f"the ladder went {at_five} then {at_thirteen}"
        )
    if run.refunded_cents != 0:
        failures.append("an expired approval let money move")
    if state.status != "waiting_approval":
        failures.append(
            f"an expired approval ended the run {state.status!r} instead of "
            f"re-requesting"
        )


def correction(failures: list[str]) -> None:
    """The approver edits 24,000 to 8,400. The same edit, and it proceeds."""
    print("\n=== correct: the outcome most systems omit ===")
    run = start_run()
    request = run.pending[0]
    new_request, decision = run.inbox.correct(
        request.id,
        by="specialist:kim",
        arguments={"amount_cents": CORRECTED},
        note="one speaker damaged, not two",
    )
    print(f"was            : {AMOUNT} cents, {request.fingerprint[:16]}")
    print(f"now            : {CORRECTED} cents, "
          f"{new_request.fingerprint[:16]}")
    print(f"attributed to  : {decision.by}")
    print(f"events         : {[e['event'] for e in run.inbox.events]}")

    state = run.loop.resume(replan(run.state, CORRECTED))
    print(f"after resume   : {state.status}")
    print(f"ledger         : {run.refund_rows} row(s), "
          f"{run.refunded_cents} cents")

    if run.refunded_cents != CORRECTED:
        failures.append(
            f"the corrected call refunded {run.refunded_cents}, "
            f"want {CORRECTED}"
        )
    if "corrected" not in [e["event"] for e in run.inbox.events]:
        failures.append("the correction was recorded as an ordinary approval")


def version_bump(failures: list[str]) -> None:
    """A tool version bump invalidates a parked approval, with a diff."""
    print("\n=== a tool version bump invalidates a parked approval ===")
    run = start_run()
    request = run.pending[0]
    run.inbox.approve(request.id, by=APPROVER)
    was = run.guard.tool_versions.version("issue_refund")
    now = run.guard.tool_versions.bump("issue_refund")
    print(f"tool version   : {was} -> {now}")

    state = run.loop.resume(run.state)
    print(f"after resume   : {state.status}")
    print(f"ledger         : {run.refund_rows} row(s), "
          f"{run.refunded_cents} cents")
    print(f"re-requested   : {[p.id for p in run.inbox.pending()]}")

    if run.refunded_cents != 0:
        failures.append("a bumped tool version still used the old approval")
    if state.status != "waiting_approval":
        failures.append(
            f"a bumped tool version ended the run {state.status!r}"
        )


def caps(failures: list[str]) -> None:
    """Hard caps raise. Nobody has to be available for them to work."""
    print("\n=== hard caps: they raise, they do not warn ===")
    run = start_run(max_writes=1)
    request = run.pending[0]
    run.inbox.approve(request.id, by=APPROVER)
    # This run already committed its one allowed write earlier in the
    # trajectory. The cap is a property of the run, not of this call, which
    # is exactly the fan-out case a turn limit misses.
    run.guard.budget.record_write("NR-2026-0041827")
    raised = ""
    try:
        run.loop.resume(run.state)
    except BudgetExceeded as exc:
        raised = str(exc)
    print(f"raised         : {raised or 'nothing, which is the bug'}")
    print(f"ledger         : {run.refund_rows} row(s)")

    if not raised:
        failures.append("the write cap did not raise")
    if run.refunded_cents != 0:
        failures.append("money moved after the write cap was exhausted")


def never_permitted(failures: list[str]) -> None:
    """The fourth class is a capability you do not grant."""
    print("\n=== never permitted: refused by the schema, not by a human ===")
    run = start_run()
    result = refund_to_non_payer(
        run.loop.tools, ORDER, AMOUNT, "ACCT-SOMEONE-ELSE"
    )
    print(f"ok             : {result.ok}")
    print(f"error          : {result.error}")
    print(f"retryable      : {result.retryable}")

    if result.ok:
        failures.append("a refund to a non-payer account was accepted")


def action_classes() -> None:
    """The assignment table, and why the class is not the tool."""
    print("\n=== action classes ===")
    for name, klass in ACTION_CLASSES.items():
        sampled = f", {klass.sample_rate:.0%} sampled" if klass.sample_rate \
            else ""
        print(f"  {name:<24} {klass.name}{sampled} "
              f"({klass.reversibility})")
    small = class_for(refund_call(APPROVAL_THRESHOLD_CENTS - 1,
                                  "NR-2026-0041827"))
    large = class_for(refund_call(APPROVAL_THRESHOLD_CENTS,
                                  "NR-2026-0041827"))
    print(f"  same tool, {APPROVAL_THRESHOLD_CENTS - 1}c -> {small.name}")
    print(f"  same tool, {APPROVAL_THRESHOLD_CENTS}c -> {large.name}")


def ladder(failures: list[str]) -> None:
    """Five rungs, a role on each, and friction that falls as you climb."""
    print("\n=== the containment ladder ===")
    log = ContainmentLog()
    log.deny_call("issue_refund", "amount above threshold, no approval")
    log.per_run_budget(RUN_ID, "writes")
    log.pause_agent("northstar-support-agent", "read_only", by="sre:oncall")
    log.roll_back_version("northstar-support-agent", "v1.7.0",
                          by="release:owner")
    log.fleet_kill_switch(by="security:oncall", reason="fleet-wide anomaly")
    for record in log.records:
        print(f"  {record['rung']:<20} stops {record['stops']:<24} "
              f"friction={record['friction']} "
              f"latency={record['latency_seconds']}s")
    print(f"  writes allowed after rung 5: "
          f"{log.writes_allowed('northstar-support-agent')}")

    tripwire = Tripwire("injection-classifier", raises_to="read_only")
    pulled = tripwire.fire(log, "northstar-support-agent", "unusual sequence")
    print(f"  a tripwire fires -> {pulled} (never 'allow')")

    if not friction_decreases():
        failures.append("authorization friction rises as you climb the ladder")
    if untested():
        failures.append(f"untested or unmeasured rungs: {untested()}")
    if log.writes_allowed("northstar-support-agent"):
        failures.append("writes still allowed after the fleet kill switch")


def main(argv: list[str] | None = None) -> int:
    args = argv if argv is not None else sys.argv[1:]
    fatal_tamper = "--fail-on-tamper" in args

    print("Chapter 20 — an approval that binds one call, in one run")
    print(f"order {ORDER}, {AMOUNT} cents, threshold "
          f"{APPROVAL_THRESHOLD_CENTS} cents")

    failures: list[str] = []
    ending_approve(failures)
    ending_tamper(failures, fatal_tamper)
    ending_expire(failures)
    correction(failures)
    version_bump(failures)
    caps(failures)
    never_permitted(failures)
    action_classes()
    ladder(failures)

    print("\n--- what this proves ---")
    print("A recorded approval binds one exact call, in one run, for a")
    print("bounded time. A modified call is rejected by the mechanism")
    print("rather than by anyone's vigilance, and the run's hard caps stop")
    print("it without a human being available at all.")

    if failures:
        print("\nFAILED:")
        for line in failures:
            print(f"  - {line}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
