"""A refund that waits three days, and a worker that really does restart.

    python artifacts/ch08-long-horizon/demo.py
    python artifacts/ch08-long-horizon/demo.py --broken-keys
    python artifacts/ch08-long-horizon/demo.py --deploy-between

The restart is not simulated in-process. This script invokes itself as a
subprocess once per phase, so the Python interpreter genuinely exits between
the pause, the decision, and the resume, and everything the next phase knows
it read back out of two SQLite files.

Four phases, and the third one dies on purpose:

1. ``start``  -- the run reaches a 24,000-cent refund on a fraud-flagged
   order, policy returns ``REQUIRE_APPROVAL``, and the pause *returns*. The
   process exits holding no lease.
2. ``decide`` -- an operator answers the request on the ``fraud-review``
   queue. An ordinary write with an identity attached.
3. ``resume`` -- a different worker rehydrates the run, runs its four checks
   in order, and is killed between the refund's intent record and its
   outcome record. The money has moved and nothing has recorded that it did.
4. ``resume`` again -- a third worker recomputes the key, asks the refund
   service whether it already settled, and finishes.

Then ``report`` prints the transition log with timestamps and checks the
ledger. Exits non-zero if the side-effect ledger holds a duplicate.

``--broken-keys`` changes one argument: the key becomes a nonce created at
call time rather than a function of ``(run_id, step_id)``. Phase 4 then
presents an identity the refund service has never seen, and the ledger for
NR-2026-0042110 ends up holding two refunds totalling 48,000 cents against a
24,000-cent claim -- plus a second copy of Friday's notice, which is the
duplicate message from the opening incident.

``--deploy-between`` runs the resume as agent v8 against a checkpoint written
by v7. The declared transformer fires, adds the field the Saturday release
added, and that additive change rewrites the pending call's canonical JSON,
which invalidates the approval a human already gave. The run returns to
``waiting_approval`` with a diff instead of raising, an operator approves the
call as it now stands, and it finishes with exactly one refund.
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import wiring  # noqa: E402
from envelope import ConfigDrift  # noqa: E402
from migrate import V7, V8  # noqa: E402
from northstar_runtime import SimulatedCrash  # noqa: E402
from pause import AMOUNT_CENTS, APPROVAL_QUEUE, ORDER, RUN_ID  # noqa: E402
from resume import RESUME_CHECKS, resume  # noqa: E402
from states import RunPhase, holds_compute  # noqa: E402

HERE = Path(__file__).resolve().parent
#: Outside the repository on purpose: three processes need to share it, and
#: nothing a demo writes belongs in a working tree.
STATE_DIR = Path(tempfile.gettempdir()) / "northstar-ch08-demo"
APPROVER = "rota:fraud-review"


def phase_start(
    state_dir: Path,
    strategy: str,
    version: str,
    label: str = "phase 1",
) -> int:
    """Reach the refund, park in front of it, and release the worker."""
    print(f"--- {label}: start (agent {version}, keys {strategy})")
    with wiring.build(
        state_dir, agent_version=version, key_strategy=strategy
    ) as wired:
        envelope = wired.workflow.start()
        print(f"  run           {envelope.run_id}")
        print(f"  order         {ORDER}, {AMOUNT_CENTS} cents requested")
        print(f"  phase         {envelope.phase.value}")
        print(f"  holds compute {holds_compute(envelope.phase)}")
        request = wired.approvals.pending(envelope.run_id)
        assert request is not None
        print(f"  parked call   {request.tool} {request.arguments}")
        print(f"  request       {request.id} on queue {request.queue}")
        print(f"  fingerprint   {request.fingerprint[:16]}...")
        print(f"  messages sent {len(wired.service.settlements(kind='message'))}")
        print(f"  refunds       {wired.service.total_cents(ORDER)} cents")
        if envelope.phase is not RunPhase.WAITING_APPROVAL:
            print(f"FAILED: the run did not park; it is {envelope.phase.value}")
            return 1
    print("  process exits here. The run costs storage and nothing else.")
    return 0


def phase_decide(
    state_dir: Path,
    version: str,
    *,
    reject: bool,
    label: str = "phase 2",
) -> int:
    """A human answers, on a queue rather than by name."""
    print(f"--- {label}: decide (by {APPROVER})")
    with wiring.build(state_dir, agent_version=version) as wired:
        inbox = wired.approvals.inbox(APPROVAL_QUEUE)
        print(f"  inbox         {len(inbox)} open request(s)")
        if not inbox:
            print("FAILED: nothing waiting on the fraud-review queue")
            return 1
        decided = wired.approvals.decide(
            inbox[0].id,
            approved=not reject,
            by=APPROVER,
            note="verified card matches the shipping address",
        )
        print(f"  decision      {decided.render()}")
        print(f"  bound to      {decided.fingerprint[:16]}...")
    print("  process exits here.")
    return 0


def phase_resume(
    state_dir: Path,
    strategy: str,
    version: str,
    *,
    kill: bool,
    label: str,
) -> int:
    """Rehydrate on a worker that did not start the run."""
    print(f"--- {label}: resume (agent {version}, keys {strategy}, "
          f"kill={kill})")
    print(f"  checks        {' -> '.join(RESUME_CHECKS)}")
    with wiring.build(
        state_dir,
        agent_version=version,
        key_strategy=strategy,
        kill_after_settle=kill,
    ) as wired:
        try:
            outcome = resume(RUN_ID, version, wired.workflow)
        except SimulatedCrash as exc:
            print(f"  killed        {exc}")
            print("  the refund settled and no outcome record landed.")
            return 0
        except ConfigDrift as exc:
            print(f"  refused       {exc}")
            return 0
        print(f"  version plan  {outcome.version_plan}")
        print(f"  outcome       {outcome.outcome}: {outcome.reason}")
        print(f"  phase         {outcome.phase.value}")
        for entry in outcome.replayed:
            print(
                f"  replayed      step {entry['step_id']} {entry['tool']} "
                f"key={entry['key'][:12]}... "
                f"already_done={entry['duplicate']}"
            )
        for key in outcome.resolved:
            print(f"  resolved      key={key[:12]}... by an independent read")
        if outcome.diff:
            for field_name, change in outcome.diff.items():
                print(
                    f"  diff          {field_name}: "
                    f"{change['approved']!r} -> {change['now']!r}"
                )
    print("  process exits here.")
    return 0


def phase_report(state_dir: Path, version: str, expect_refunds: int) -> int:
    """The transition log, the intent journal, and the ledger."""
    print("--- report")
    failures: list[str] = []
    with wiring.build(state_dir, agent_version=version) as wired:
        print("  state transitions, in order, with timestamps:")
        for transition in wired.store.history(RUN_ID):
            print(f"    {transition.render()}")

        print("\n  intent journal (yours):")
        for intent in wired.ledger.intents(RUN_ID):
            print(f"    {intent.render()}")

        refunds = wired.service.settlements(order_id=ORDER, kind="refund")
        messages = wired.service.settlements(kind="message")
        total = wired.service.total_cents(ORDER)
        print("\n  refund service (not yours):")
        for row in refunds:
            print(
                f"    {row['settlement_id']} {row['amount_cents']:>6}c "
                f"key={row['idempotency_key'][:12]}..."
            )
        print(f"  refund rows   {len(refunds)}")
        print(f"  refunded      {total} cents against a "
              f"{AMOUNT_CENTS}-cent claim")
        print(f"  messages      {len(messages)}")
        print(f"  parked runs   {wired.store.parked()}")

        envelope = wired.store.load_envelope(RUN_ID)
        print(f"  final phase   {envelope.phase.value}")

        if len(refunds) != expect_refunds:
            failures.append(
                f"the ledger holds {len(refunds)} refund row(s), "
                f"expected {expect_refunds}"
            )
        if expect_refunds == 1 and total != AMOUNT_CENTS:
            failures.append(f"the ledger holds {total}c, want {AMOUNT_CENTS}c")
        if expect_refunds == 1 and len(messages) != 1:
            failures.append(
                f"the customer received {len(messages)} copies of the notice"
            )
        if wired.ledger.unresolved(RUN_ID):
            failures.append(
                "an intent still has no outcome after the run finished"
            )

    if failures:
        print("\nFAILED:")
        for line in failures:
            print(f"  - {line}")
        return 1
    return 0


def run_phase(args: list[str]) -> int:
    """Invoke one phase in a fresh interpreter and return its exit code.

    The flush matters: this process's stdout is a pipe under ``make`` and in
    CI, so without it the orchestrator's own lines arrive after every
    child's and the transcript reads back to front.
    """
    sys.stdout.flush()
    completed = subprocess.run(
        [sys.executable, str(HERE / "demo.py"), *args],
        check=False,
    )
    sys.stdout.flush()
    return completed.returncode


def orchestrate(broken_keys: bool, deploy_between: bool) -> int:
    """Drive the phases as separate processes, then check the ledger."""
    strategy = "generated" if broken_keys else "derived"
    resume_version = V8 if deploy_between else V7
    # The nonce path pays twice: once before the kill under a key nobody
    # recorded, and once after it under a key nobody has seen.
    expect_refunds = 2 if broken_keys else 1

    print("Chapter 8 — a run that outlives its process")
    print(f"state         {STATE_DIR}")
    print(f"keys          {strategy}")
    print(f"resume as     {resume_version}")
    print()
    wiring.reset(STATE_DIR)

    common = ["--state", str(STATE_DIR)]
    if broken_keys:
        common.append("--broken-keys")

    plan: list[list[str]] = [
        ["--phase", "start", "--agent-version", V7],
        ["--phase", "decide", "--agent-version", V7],
        ["--phase", "resume", "--agent-version", resume_version, "--kill"],
    ]
    if deploy_between:
        # The migration invalidates the approval, so the run comes back
        # parked. An operator answers the re-request and a later worker
        # finishes it. Two extra processes, both ordinary.
        plan.append(["--phase", "decide", "--agent-version", resume_version])
        plan.append(
            ["--phase", "resume", "--agent-version", resume_version, "--kill"]
        )
    plan.append(["--phase", "resume", "--agent-version", resume_version])

    codes = [
        run_phase([*common, *step, "--label", f"phase {n}"])
        for n, step in enumerate(plan, start=1)
    ]
    codes.append(
        run_phase(
            [
                *common,
                "--phase",
                "report",
                "--agent-version",
                resume_version,
                "--expect-refunds",
                str(expect_refunds),
            ]
        )
    )

    print("\n--- what this proves ---")
    print("A run that paused for a human decision, lost its process")
    print("entirely, and came back under a different worker performed each")
    print("side effect exactly once. The specific mechanisms are the")
    print("checkpoint written before the pause returned, the key")
    print("recomputed rather than loaded, and the version compared before")
    print("anything was deserialised.")
    if broken_keys:
        print()
        print("With --broken-keys none of that holds, and the ledger says")
        print("so: two refunds and two copies of one notice.")

    failed = [i for i, code in enumerate(codes, start=1) if code != 0]
    if failed:
        print(f"\nFAILED: phase(s) {failed} exited non-zero")
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    args = list(argv if argv is not None else sys.argv[1:])
    broken_keys = "--broken-keys" in args
    strategy = "generated" if broken_keys else "derived"

    if "--phase" not in args:
        return orchestrate(broken_keys, "--deploy-between" in args)

    phase = args[args.index("--phase") + 1]
    state_dir = (
        Path(args[args.index("--state") + 1])
        if "--state" in args
        else STATE_DIR
    )
    version = (
        args[args.index("--agent-version") + 1]
        if "--agent-version" in args
        else V7
    )
    label = (
        args[args.index("--label") + 1] if "--label" in args else "phase"
    )
    if phase == "start":
        return phase_start(state_dir, strategy, version, label)
    if phase == "decide":
        return phase_decide(
            state_dir, version, reject="--reject" in args, label=label
        )
    if phase == "resume":
        return phase_resume(
            state_dir,
            strategy,
            version,
            kill="--kill" in args,
            label=label,
        )
    if phase == "report":
        expect = (
            int(args[args.index("--expect-refunds") + 1])
            if "--expect-refunds" in args
            else 1
        )
        return phase_report(state_dir, version, expect)
    print(f"unknown phase {phase!r}; expected start, decide, resume, report")
    return 2


if __name__ == "__main__":
    sys.exit(main())
