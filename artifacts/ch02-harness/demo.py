"""Kill a refund run mid-write, resume it in a fresh process, read the ledger.

    python artifacts/ch02-harness/demo.py

Three runs, one script, one refund service:

1. **Correct placement.** Intent journaled before the dispatch, evidence
   after, key derived from the run and the step. The worker is killed the
   instant the refund lands. A second Python process, which has never seen
   the first one's memory, resumes from the SQLite file and finishes. Ledger:
   one refund.
2. **Wrong placement.** The checkpoint sits between the model's decision and
   the dispatch, and the call carries no key. Same fault, same kill. Ledger:
   two refunds, and a run that reports ``succeeded``.
3. **The worksheet.** The eight autonomy axes, each checked against the live
   component that enforces it, plus the six-condition suitability gate.

Exits non-zero if the resumed run produces a second refund, if the wrong
placement fails to produce one, or if any axis in ``autonomy_budget.yaml``
has no enforcement point.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import argparse
import os
import subprocess
import tempfile
from dataclasses import dataclass

import checkpoint_wrong
from autonomy import AutonomyPolicy, Wiring, guard_for, load_budget, unenforced
from checkpoint import SqliteCheckpointer, config_hash_for
from journal import StepJournal
from loop import HarnessLoop, WorkerKilled, killed_after
from northstar_contracts import ToolCall, World
from northstar_runtime import FakeModel
from refund_ledger import RefundLedger
from registry import HarnessRegistry
from runner import ConfigDrift, HarnessRunner, UnknownRun
from suitability import ADDRESS_CHANGE, REFUND_TRIAGE, assess

ORDER = "NR-2026-0041827"   # US$84.00, delivered, two items
SKU = "NR-LAMPSHADE-03"
AMOUNT = 3250               # cents. Below the 5000-cent approval threshold.
CUSTOMER = "CUST-8841"
GOAL = "Customer says the lamp shade in this order arrived cracked."
RUN_ID = "run_ch02_refund"
SYSTEM_PROMPT = "You are the Northstar Returns support agent."


def script() -> list[object]:
    """Read in parallel, write in sequence, then answer.

    Turn one asks for both reads at once, which is the safe kind of
    parallelism: reads commute. The refund is alone in turn two, because
    writes do not.
    """
    return [
        [
            ToolCall("c1", "get_order", {"order_id": ORDER}),
            ToolCall("c2", "get_policy", {"sku": SKU, "reason": "damaged"}),
        ],
        ToolCall("c3", "issue_refund", {"order_id": ORDER,
                                        "amount_cents": AMOUNT,
                                        "reason": "damaged"}),
        ToolCall("c4", "send_message", {"order_id": ORDER,
                                        "body": "Refunded the lamp shade. "
                                                "Sorry it arrived cracked."}),
        "Refunded 3250 cents for the cracked lamp shade.",
    ]


def owners(world: World) -> dict[str, str]:
    """Order id to owning customer, for the resource-scope decision.

    Read once, at admission. A policy decision point that has to query the
    system it is protecting fails open when that system is down.
    """
    return {oid: o["customer_id"] for oid, o in world.orders.items()}


@dataclass
class Worker:
    """One process's view of one run.

    A worker is not a run. Two of these, built from the same file, are what
    the resume demo is about: they share a checkpoint, a journal, and a
    refund ledger, and nothing else.
    """

    runner: HarnessRunner
    service: RefundLedger
    policy: AutonomyPolicy

    def close(self) -> None:
        """Release both database connections."""
        self.service.close()
        self.runner.close()

    def __enter__(self) -> Worker:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def wire(
    db: Path,
    *,
    inject_timeout: bool,
    kill_on_refund: bool,
    unsafe: bool = False,
) -> Worker:
    """Assemble a worker: world, refund service, policy, guard, loop.

    Args:
        db: SQLite file holding both the checkpoints and the refund
            receipts. One file, so "delete the state" is one command.
        inject_timeout: Make the refund commit and then time out, which is
            the only failure a caller cannot interpret.
        kill_on_refund: End the process the moment the refund lands.
        unsafe: Use the wrong-order loop and an unkeyed registry.
    """
    world = World()
    service = RefundLedger(world, db)
    service.hydrate()
    if inject_timeout:
        world.inject_fault("issue_refund", kind="timeout")

    budget = load_budget()
    policy = AutonomyPolicy(budget, CUSTOMER, owners(world))
    tools = HarnessRegistry(policy=policy).register_all(service.tools())
    if unsafe:
        tools = checkpoint_wrong.unkeyed(tools)

    config_hash = config_hash_for(
        model="fake-model-1",
        system_prompt=SYSTEM_PROMPT,
        specs=tools.specs(),
    )
    checkpointer = SqliteCheckpointer(db, config_hash=config_hash)
    journal = StepJournal.on_file(RUN_ID, db.with_suffix(".jsonl"))
    guard = guard_for(budget, journal)
    dispatch = (
        killed_after(tools, lambda call: call.name == "issue_refund")
        if kill_on_refund
        else tools
    )
    loop_class = checkpoint_wrong.WrongOrderLoop if unsafe else HarnessLoop
    loop = loop_class(
        FakeModel(default=script()),
        dispatch,
        checkpointer=checkpointer,
        journal=journal,
        budget=guard,
        system_prompt=SYSTEM_PROMPT,
    )
    runner = HarnessRunner(loop, checkpointer, journal, dispatch, config_hash)
    return Worker(runner, service, policy)


# ----------------------------------------------------------- the three runs


def run_correct(db: Path) -> dict[str, object]:
    """Start, die on the refund, resume in a second process, read the ledger."""
    killed = ""
    with wire(db, inject_timeout=True, kill_on_refund=True) as worker:
        try:
            worker.runner.start(GOAL, RUN_ID)
        except WorkerKilled as exc:
            killed = str(exc)
        before = worker.service.rows(ORDER)

    resumed = subprocess.run(
        [sys.executable, str(Path(__file__).resolve()), "--resume", str(db)],
        capture_output=True,
        text=True,
        check=False,
    )

    after = RefundLedger(World(), db)
    rows = after.rows(ORDER)
    after.close()
    return {
        "killed": killed,
        "ledger_at_kill": before,
        "ledger_after_resume": rows,
        "child_stdout": resumed.stdout.strip(),
        "child_returncode": resumed.returncode,
    }


def resume_in_this_process(db: Path) -> str:
    """The second worker. Nothing of the first one's memory survives here."""
    with wire(db, inject_timeout=False, kill_on_refund=False) as worker:
        state = worker.runner.resume(RUN_ID)
        settled = ", ".join(worker.runner.settled) or "nothing"
        rows = len(worker.service.rows(ORDER))
    return (
        f"resumed {RUN_ID} on pid {os.getpid()}: "
        f"status={state.status} step={state.step} "
        f"settled={settled} ledger_rows={rows}"
    )


def run_wrong(db: Path) -> dict[str, object]:
    """The same kill against the boundary that loses the outcome."""
    with wire(
        db, inject_timeout=True, kill_on_refund=True, unsafe=True
    ) as first:
        try:
            first.runner.start(GOAL, RUN_ID)
        except WorkerKilled:
            pass

    with wire(
        db, inject_timeout=False, kill_on_refund=False, unsafe=True
    ) as second:
        state = checkpoint_wrong.resume_from_history(
            second.runner.loop, second.runner.checkpointer, RUN_ID
        )
        rows = second.service.rows(ORDER)
    return {"status": state.status, "ledger_after_resume": rows}


def run_worksheet() -> list[str]:
    """Check every axis against the component that is supposed to hold it."""
    world = World()
    budget = load_budget()
    policy = AutonomyPolicy(budget, CUSTOMER, owners(world))
    tools = HarnessRegistry(policy=policy).register_all(world.tools())
    guard = guard_for(budget)
    return unenforced(budget, Wiring(guard, tools, policy))


# --------------------------------------------------------------- reporting


def report_run(label: str, result: dict[str, object]) -> None:
    """Print one run's kill point and both ledger readings."""
    print(f"\n=== {label} ===")
    for key, value in result.items():
        if isinstance(value, list):
            total = sum(int(r["amount_cents"]) for r in value)
            print(f"{key:22}: {len(value)} refund(s), {total} cents")
        else:
            print(f"{key:22}: {value}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--resume",
        metavar="DB",
        help="internal: act as the second worker for the run in DB",
    )
    args = parser.parse_args()

    if args.resume:
        print(resume_in_this_process(Path(args.resume)))
        return 0

    failures: list[str] = []
    with tempfile.TemporaryDirectory(prefix="ch02-") as tmp:
        correct = run_correct(Path(tmp) / "right.sqlite")
        report_run("intent before dispatch, evidence after", correct)
        wrong = run_wrong(Path(tmp) / "wrong.sqlite")
        report_run("checkpoint between decision and outcome", wrong)

        n_correct = len(correct["ledger_after_resume"])   # type: ignore[arg-type]
        n_wrong = len(wrong["ledger_after_resume"])       # type: ignore[arg-type]
        if n_correct != 1:
            failures.append(
                f"the resumed run should leave one refund, left {n_correct}"
            )
        if n_wrong != 2:
            failures.append(
                f"the wrong boundary should double-pay, left {n_wrong}"
            )
        if correct["child_returncode"] != 0:
            failures.append("the second worker did not exit cleanly")

    print("\n=== a resume this process refuses ===")
    with tempfile.TemporaryDirectory(prefix="ch02-") as tmp:
        db = Path(tmp) / "drift.sqlite"
        with wire(db, inject_timeout=False, kill_on_refund=False) as first:
            first.runner.start(GOAL, RUN_ID)
        with wire(db, inject_timeout=False, kill_on_refund=False) as second:
            second.runner.config_hash = "a-different-prompt"
            cases = ((RUN_ID, ConfigDrift), ("run_never", UnknownRun))
            for run_id, expected in cases:
                try:
                    second.runner.resume(run_id)
                except (ConfigDrift, UnknownRun) as exc:
                    print(f"{type(exc).__name__:12}: {exc}")
                else:
                    failures.append(f"{expected.__name__} was not raised")

    print("\n=== autonomy budget: eight axes, eight enforcement points ===")
    problems = run_worksheet()
    for line in problems or ["every axis is read by a live component"]:
        print(f"  {line}")
    failures.extend(problems)

    print("\n=== suitability gate ===")
    for name, answers in (
        ("damaged-item triage", REFUND_TRIAGE),
        ("address change", ADDRESS_CHANGE),
    ):
        verdict = assess(answers)
        print(f"  {name}: agent={verdict.build_an_agent}")
        for line in verdict.report():
            print(f"    {line}")
    if not assess(REFUND_TRIAGE).build_an_agent:
        failures.append("the gate rejected the task the chapter says fits")
    if assess({}).build_an_agent:
        failures.append("the gate passed an empty worksheet")

    print("\n--- what this proves ---")
    print("A run interrupted at its most dangerous moment resumed to a")
    print("correct world state, and the property came from where the two")
    print("journal writes sit plus a derived key, not from SQLite.")

    if failures:
        print("\nFAILED:")
        for line in failures:
            print(f"  - {line}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
