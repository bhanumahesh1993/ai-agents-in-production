"""The crash harness: start, die, resume, and count the refunds.

Four injection points, each producing a distinguishable journal. The
interesting one is ``after_refund_commit``: the effect landed, the outcome
record did not, and the resumed run finds an intent with no answer. That is
the only state from which you can do something sensible, and what it does
is re-issue the call under the same derived key.

Nothing here exits the interpreter. The chapter's harness exits the process
for real; a chapter demo that killed its own interpreter could not then
print what happened, and CI could not tell a demonstration apart from a
crash. :class:`~northstar_runtime.SimulatedCrash` unwinds the stack instead,
which reaches the same journal state — the records written before the raise
are already durable — and leaves something to read afterwards.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from northstar_contracts import RunState, World
from northstar_runtime import (
    FileJournal,
    Journal,
    MemoryJournal,
    SimulatedCrash,
)
from workflow import RunContext, Suspended, refund_workflow

__all__ = [
    "FLAGGED_CENTS",
    "FLAGGED_ORDER",
    "LAMP_SHADE_CENTS",
    "ORDER",
    "DurableRun",
    "resume",
    "start",
    "trace",
]

ORDER = "NR-2026-0041827"       # US$84.00, delivered, two items
LAMP_SHADE_CENTS = 3250         # under the threshold, no human needed
FLAGGED_ORDER = "NR-2026-0042110"
FLAGGED_CENTS = 24000           # over the threshold, a human decides


@dataclass
class DurableRun:
    """One attempt at a run, and what it left behind.

    Args:
        run_id: The run's identity.
        journal: The append-only record, shared across attempts.
        world: The target system, shared across attempts. The refund
            service outlives the worker, which is the only reason an
            idempotency key is worth anything.
        state: The terminal state, or ``None`` if the attempt crashed or
            suspended.
        outcome: ``finished``, ``crashed``, or ``suspended``.
    """

    run_id: str
    journal: Journal
    world: World
    state: RunState | None = None
    outcome: str = "finished"
    detail: str = ""
    executed: list[str] = field(default_factory=list)
    replayed: list[str] = field(default_factory=list)

    @property
    def refund_rows(self) -> int:
        """How many refunds landed. The number that must stay 1."""
        return len(self.world.refunds_for(self.order_id))

    @property
    def refunded_cents(self) -> int:
        """What the ledger holds, not what the run said."""
        return self.world.total_refunded_cents(self.order_id)

    @property
    def order_id(self) -> str:
        """Which order this run touched."""
        for record in self.journal.records(self.run_id):
            args = record["payload"].get("args") or []
            if args and isinstance(args[0], str) and args[0].startswith("NR-"):
                return args[0]
        return ORDER

    def records(self) -> list[dict[str, Any]]:
        """Every journal record for this run, in order."""
        return self.journal.records(self.run_id)

    def unresolved(self) -> list[str]:
        """Intents with no outcome. The question a resume has to answer."""
        completed = {
            r["payload"]["step_id"]
            for r in self.records()
            if r["type"] == "step.completed"
        }
        return [
            r["payload"]["step_id"]
            for r in self.records()
            if r["type"] == "step.started"
            and r["payload"]["step_id"] not in completed
        ]


def _attempt(
    run_id: str,
    journal: Journal,
    world: World,
    order_id: str,
    amount_cents: int,
    *,
    crash_at: str | None,
    unsafe_key: bool,
    unsafe_clock: bool,
) -> DurableRun:
    """Run the workflow once against an existing journal."""
    ctx = RunContext(
        run_id=run_id,
        journal=journal,
        world=world,
        crash_at=crash_at,
        unsafe_key=unsafe_key,
        unsafe_clock=unsafe_clock,
    )
    run = DurableRun(run_id, journal, world)
    try:
        run.state = refund_workflow(ctx, order_id, amount_cents)
    except SimulatedCrash as exc:
        run.outcome, run.detail = "crashed", str(exc)
    except Suspended as exc:
        run.outcome, run.detail = "suspended", str(exc)
    run.executed = list(ctx.executed)
    run.replayed = list(ctx.replayed)
    return run


def start(
    run_id: str,
    *,
    order_id: str = ORDER,
    amount_cents: int = LAMP_SHADE_CENTS,
    crash_at: str | None = None,
    unsafe_key: bool = False,
    unsafe_clock: bool = False,
    journal_path: str | None = None,
    world: World | None = None,
) -> DurableRun:
    """Begin a durable run, optionally crashing at a named point."""
    journal: Journal = (
        FileJournal(journal_path) if journal_path else MemoryJournal()
    )
    return _attempt(
        run_id,
        journal,
        world if world is not None else World(),
        order_id,
        amount_cents,
        crash_at=crash_at,
        unsafe_key=unsafe_key,
        unsafe_clock=unsafe_clock,
    )


def resume(
    run: DurableRun,
    *,
    order_id: str | None = None,
    amount_cents: int = LAMP_SHADE_CENTS,
    approve: bool = False,
    approver: str = "rota:fraud-review",
    unsafe_key: bool = False,
    unsafe_clock: bool = False,
) -> DurableRun:
    """Continue a crashed or suspended run on a *different* context.

    A fresh :class:`~workflow.RunContext` is built over the same journal
    and the same world, which is what a second worker picking the run up
    actually has: the record, and the service. It does not have the first
    worker's memory, and nothing here pretends otherwise.
    """
    if approve:
        decider = RunContext(
            run_id=run.run_id, journal=run.journal, world=run.world
        )
        decider.record_decision("refund_approval", True, approver)
    return _attempt(
        run.run_id,
        run.journal,
        run.world,
        order_id or run.order_id,
        amount_cents,
        crash_at=None,
        unsafe_key=unsafe_key,
        unsafe_clock=unsafe_clock,
    )


def trace(run: DurableRun) -> list[str]:
    """The journal, in the shape the chapter prints."""
    lines: list[str] = []
    attempted: set[str] = set()
    for record in run.records():
        payload = record["payload"]
        seq = record["seq"]
        kind = record["type"]
        step = payload.get("step_id", "")
        short = step.rsplit(":", 1)[-1]
        if kind == "step.started":
            # A second intent for the same step is a *resolution*, not a
            # repetition: the run found an intent with no outcome and
            # re-issued it under the same derived key.
            resolving = "  (resolving)" if short in attempted else ""
            attempted.add(short)
            lines.append(
                f"{run.run_id}  seq={seq}  step.started    "
                f"{short}{resolving}"
            )
        elif kind == "step.completed":
            duplicate = (
                "  dup, receipt replayed"
                if isinstance(payload.get("result"), dict)
                and payload["result"].get("duplicate")
                else ""
            )
            lines.append(
                f"{run.run_id}  seq={seq}  step.completed  {short}{duplicate}"
            )
        elif kind == "clock.read":
            lines.append(
                f"{run.run_id}  seq={seq}  clock.read      "
                f"now={payload['now']:.0f}"
            )
        else:
            lines.append(
                f"{run.run_id}  seq={seq}  approval.await  {short}"
            )
    if run.outcome == "crashed":
        lines.append("--- process exit (injected) ---")
    return lines
