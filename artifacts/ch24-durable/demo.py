"""A durable runner, four injected crashes, and a stream that survives them.

    python artifacts/ch24-durable/demo.py
    python artifacts/ch24-durable/demo.py --unsafe-key
    python artifacts/ch24-durable/demo.py --unsafe-clock
    python artifacts/ch24-durable/demo.py --replay-test

Runs the workflow with a crash injected between the refund intent and
outcome records, prints the journal, and exits non-zero if the ledger
contains a duplicate. ``--unsafe-key`` watches the duplicate appear,
``--unsafe-clock`` watches a value diverge silently across a replay, and
``--replay-test`` replays the shipped journal corpus against the current
build.

Also shown: the same crash-and-resume through ``DurableRunner``, the
book's loop-shaped engine, so both halves of the contract are visible;
suspension across a human approval; and a reconnecting SSE client that
receives what it missed without a gap or a duplicate.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import corpus
import crash
import unsafe
from northstar_contracts import World
from northstar_runtime import (
    DurableRunner,
    FakeModel,
    ReplayDivergence,
    SimulatedCrash,
)
from stream import LAST_EVENT_ID_HEADER, StreamClient, stream
from workflow import CRASH_POINTS


def crash_and_resume(failures: list[str]) -> None:
    """The one that matters: a crash inside the money-moving window."""
    print("\n=== a crash between the refund intent and its outcome ===")
    first = crash.start("run_01HQ8ZK3M7", crash_at="after_refund_commit")
    for line in crash.trace(first):
        print(line)
    print(f"unresolved intents : "
          f"{[s.rsplit(':', 1)[-1] for s in first.unresolved()]}")
    print(f"ledger at the crash: {first.refund_rows} row(s), "
          f"{first.refunded_cents} cents")

    resumed = crash.resume(first)
    print()
    for line in crash.trace(resumed)[5:]:
        print(line)
    print(f"replayed           : {resumed.replayed}")
    print(f"executed live      : {resumed.executed}")
    print(f"ledger after resume: {resumed.refund_rows} row(s), "
          f"{resumed.refunded_cents} cents")
    print(f"status             : "
          f"{resumed.state.status if resumed.state else resumed.outcome}")
    print("\nthe mechanism: the replay found an intent with no outcome,")
    print("re-issued the call under the same derived key, and the refund")
    print("service returned the original receipt instead of paying again.")
    print("Resolve, do not repeat — as an ordinary retry of an idempotent")
    print("step.")

    if resumed.refund_rows != 1:
        failures.append(
            f"the resumed run left {resumed.refund_rows} refund rows"
        )
    if resumed.refunded_cents != crash.LAMP_SHADE_CENTS:
        failures.append(
            f"the ledger holds {resumed.refunded_cents}c, want "
            f"{crash.LAMP_SHADE_CENTS}c"
        )
    if resumed.replayed != ["get_order", "get_policy"]:
        failures.append(f"the replay re-ran {resumed.replayed}")


def all_four_crash_points(failures: list[str]) -> None:
    """Four points, four distinguishable journals, one refund every time."""
    print("\n=== every crash point, resumed ===")
    for point in CRASH_POINTS:
        if point == "mid_stream":
            continue  # covered by the stream section, where it belongs
        order = crash.ORDER
        amount = crash.LAMP_SHADE_CENTS
        approve = point == "during_approval_wait"
        if approve:
            order, amount = crash.FLAGGED_ORDER, crash.FLAGGED_CENTS
        first = crash.start(
            f"run_{point}", order_id=order, amount_cents=amount,
            crash_at=point,
        )
        resumed = crash.resume(
            first, order_id=order, amount_cents=amount, approve=approve
        )
        status = resumed.state.status if resumed.state else resumed.outcome
        print(f"  {point:<24} died at seq={len(first.records())}, "
              f"resumed to {status}, "
              f"{resumed.refund_rows} refund(s), {resumed.refunded_cents}c")
        if resumed.refund_rows != 1:
            failures.append(
                f"{point}: {resumed.refund_rows} refund rows after resume"
            )


def durable_runner(failures: list[str]) -> None:
    """The same contract, loop-shaped, on the book's own engine."""
    print("\n=== the same crash through DurableRunner ===")
    world = World()
    script = crash_script()
    runner = DurableRunner(
        model=FakeModel(default=script),
        tools=world.tools(),
        max_turns=8,
    )
    crashed = False
    try:
        runner.start("refund the cracked lamp shade", run_id="run-engine",
                     crash_after_step=3)
    except SimulatedCrash as exc:
        crashed = True
        print(f"  crashed: {exc}")
    print(f"  journal after crash : {len(runner.history('run-engine'))} "
          f"record(s)")
    print(f"  ledger after crash  : "
          f"{len(world.refunds_for(crash.ORDER))} row(s)")

    state = runner.resume("run-engine")
    print(f"  resumed to          : {state.status}")
    print(f"  ledger after resume : "
          f"{len(world.refunds_for(crash.ORDER))} row(s), "
          f"{world.total_refunded_cents(crash.ORDER)} cents")
    replayed = runner.replay("run-engine")
    print(f"  replay-only rebuild : {replayed.status} at step "
          f"{replayed.step}, world untouched")

    if not crashed:
        failures.append("DurableRunner did not crash where it was told to")
    if len(world.refunds_for(crash.ORDER)) != 1:
        failures.append(
            f"DurableRunner left {len(world.refunds_for(crash.ORDER))} "
            f"refund rows"
        )


def crash_script() -> list[object]:
    """A trajectory for the loop-shaped engine, with the refund at step 3."""
    from northstar_contracts import ToolCall

    return [
        ToolCall("c1", "get_order", {"order_id": crash.ORDER}),
        ToolCall("c2", "get_policy", {"reason": "damaged"}),
        ToolCall(
            "c3",
            "issue_refund",
            {
                "order_id": crash.ORDER,
                "amount_cents": crash.LAMP_SHADE_CENTS,
                "reason": "damaged",
            },
        ),
        "Refunded the cracked lamp shade.",
    ]


def suspension(failures: list[str]) -> None:
    """A wait costs storage, not a warm process."""
    print("\n=== suspend across a human, and resume ===")
    parked = crash.start(
        "run_approval",
        order_id=crash.FLAGGED_ORDER,
        amount_cents=crash.FLAGGED_CENTS,
    )
    parked_outcome = parked.outcome
    rows_while_parked = parked.refund_rows
    print(f"  outcome            : {parked_outcome}")
    print(f"  journal            : {len(parked.records())} record(s)")
    print(f"  ledger while parked: {rows_while_parked} row(s)")
    print("  cost while parked  : a row and a timer. No worker, no")
    print("                       container, no connection.")

    resumed = crash.resume(
        parked, amount_cents=crash.FLAGGED_CENTS, approve=True
    )
    print(f"  after the decision : "
          f"{resumed.state.status if resumed.state else resumed.outcome}, "
          f"{resumed.refund_rows} refund(s), {resumed.refunded_cents}c")

    if parked_outcome != "suspended":
        failures.append(f"the run did not suspend; it {parked_outcome}")
    if rows_while_parked != 0:
        failures.append("money moved before the human decided")
    if resumed.refunded_cents != crash.FLAGGED_CENTS:
        failures.append(
            f"after approval the ledger holds {resumed.refunded_cents}c"
        )


def resumable_stream(failures: list[str]) -> None:
    """Kill the connection mid-run and reconnect with Last-Event-ID."""
    print("\n=== a stream that survives the connection dying ===")
    run = crash.resume(crash.start("run_stream"))
    total = len(run.records())

    client = StreamClient(run.run_id)
    delivered = client.consume(
        stream(run.journal, run.run_id, crash_after=3)
    )
    print(f"  first connection : {delivered} event(s), "
          f"last id {client.last_event_id}")
    print(f"  reconnect header : {LAST_EVENT_ID_HEADER}: "
          f"{client.headers()[LAST_EVENT_ID_HEADER]}")

    delivered_again = client.consume(
        stream(run.journal, run.run_id, client.last_event_id)
    )
    print(f"  second connection: {delivered_again} event(s), "
          f"last id {client.last_event_id}")
    print(f"  total delivered  : {len(client.ids)} of {total}")
    print(f"  gapless, no dupes: {client.gapless}")

    if len(client.ids) != total:
        failures.append(
            f"the client saw {len(client.ids)} of {total} events"
        )
    if not client.gapless:
        failures.append("the reconnect left a gap or a duplicate")


def unsafe_key(failures: list[str]) -> None:
    """A nonce is not an idempotency key, and the difference is 3,250 cents."""
    print("\n=== --unsafe-key ===")
    first = crash.start(
        "run_unsafe_key", crash_at="after_refund_commit", unsafe_key=True
    )
    resumed = crash.resume(first, unsafe_key=True)
    print(f"  ledger for {crash.ORDER}: {resumed.refund_rows} refund(s), "
          f"{resumed.refunded_cents} cents")
    print("  the resumed run computed a different key, the refund service")
    print("  saw a new intent, and paid a second time. The step sequence")
    print("  matched throughout, so nothing detected it.")

    if resumed.refund_rows != 2:
        failures.append(
            f"--unsafe-key produced {resumed.refund_rows} refund rows; the "
            f"demonstration needs 2"
        )


def unsafe_clock(failures: list[str]) -> None:
    """The silent divergence. Same sequence, different value."""
    print("\n=== --unsafe-clock ===")
    result = unsafe.compare("run_unsafe_clock", crash.ORDER)
    print(f"  safe,   attempt 1 : {result['safe'][0]}")
    print(f"  safe,   attempt 2 : {result['safe'][1]}")
    print(f"  broken, attempt 1 : {result['broken'][0]}")
    print(f"  broken, attempt 2 : {result['broken'][1]}")
    print(f"  safe is stable    : {result['safe_is_stable']}")
    print(f"  broken is stable  : {result['broken_is_stable']}")
    print("  nothing raised. The step sequence still matches, so a value")
    print("  divergence inside a step's arguments is invisible — and when")
    print("  the value is an idempotency key it produces exactly the")
    print("  duplicate the key exists to prevent.")

    if not result["safe_is_stable"]:
        failures.append("the safe versions drifted across attempts")
    if result["broken_is_stable"]:
        failures.append("the broken versions did not drift, so the demo lies")


def replay_test(failures: list[str]) -> None:
    """The shipped corpus, against the current build."""
    print("\n=== --replay-test ===")
    for result in corpus.replay_all():
        print(f"  {result['name']:<26} -> {result['status']:<16} "
              f"replayed={len(result['replayed'])} "
              f"executed={len(result['executed'])} "
              f"world_untouched={result['world_untouched']}")
        if result["executed"]:
            failures.append(
                f"{result['name']}: a replay executed "
                f"{result['executed']}"
            )
        if not result["world_untouched"]:
            failures.append(f"{result['name']}: a replay touched the world")

    problems = corpus.diverging()
    print(f"  divergences: {problems or 'none'}")
    failures.extend(problems)

    print("\n  and a journal the current build cannot reproduce:")
    entry = corpus.load_corpus()[0]
    mutated = corpus.CorpusEntry(
        name=entry.name + "-mutated",
        run_id=entry.run_id,
        order_id=entry.order_id,
        amount_cents=entry.amount_cents,
        records=[
            {
                **record,
                "payload": {
                    **record["payload"],
                    **(
                        {"step_id": f"{entry.run_id}:check_fraud"}
                        if record["payload"].get("step_id", "").endswith(
                            "get_policy"
                        )
                        else {}
                    ),
                },
            }
            for record in entry.records
        ],
    )
    raised = ""
    try:
        corpus.replay(mutated)
    except ReplayDivergence as exc:
        raised = str(exc)
    print(f"  {raised or 'nothing raised, which is the bug'}")

    if not raised:
        failures.append("a changed step sequence did not raise loudly")


def main(argv: list[str] | None = None) -> int:
    args = argv if argv is not None else sys.argv[1:]
    print("Chapter 24 — a run continued, not run again")

    failures: list[str] = []
    if "--unsafe-key" in args:
        unsafe_key(failures)
    elif "--unsafe-clock" in args:
        unsafe_clock(failures)
    elif "--replay-test" in args:
        replay_test(failures)
    else:
        crash_and_resume(failures)
        all_four_crash_points(failures)
        durable_runner(failures)
        suspension(failures)
        resumable_stream(failures)
        unsafe_key(failures)
        unsafe_clock(failures)
        replay_test(failures)

        print("\n--- what this proves ---")
        print("A run killed at any of four points, including inside the")
        print("window where a money-moving call had started and not")
        print("finished, resumes on a different context and leaves exactly")
        print("one refund in the ledger. And the journal line where the")
        print("duplicate was recognised instead of paid is printable.")

    if failures:
        print("\nFAILED:")
        for line in failures:
            print(f"  - {line}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
