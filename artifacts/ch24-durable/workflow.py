"""The workflow half: deterministic code over journaled steps.

Every effect goes through :meth:`RunContext.step`, which journals the
intent, invokes the callable, journals the outcome, and on replay returns
the recorded result without invoking anything. What remains in workflow
code is the loop, the branch on a decision, the budget check, and the
termination test. That is a smaller surface than most agent loops start
with, and shrinking it is the work of adoption.

Three record types, and the order is the contract:

``step.started``
    The intent. Written **before** the callable runs, so a crash inside
    the window leaves an intent with no outcome — which is a question, not
    an answer, and the only state from which you can do something
    sensible.
``step.completed``
    The outcome. Only now does the effect count as done.
``clock.read``
    A journaled time. Workflow code reads neither the wall clock nor a
    random source; both come in as recorded inputs.

These are not :data:`northstar_runtime.JOURNAL_TYPES`. That set belongs to
:class:`~northstar_runtime.DurableRunner`, which journals a *loop*: model
responses and tool effects. This module journals a *function*, so it adds
its own three types and reuses everything else — ``MemoryJournal``,
``FileJournal``, ``ReplayDivergence``, and ``SimulatedCrash`` — rather than
building a second journal beside the first.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from northstar_contracts import RunState, World, idempotency_key
from northstar_runtime import (
    Journal,
    JournalExhausted,
    MemoryJournal,
    ReplayDivergence,
    SimulatedCrash,
)

__all__ = [
    "CRASH_POINTS",
    "STEP_TYPES",
    "RunContext",
    "Suspended",
    "refund_workflow",
    "step_record",
]

#: The record types this module appends. Narrow on purpose: the minimum
#: needed to reconstruct one function's progress exactly.
STEP_TYPES: frozenset[str] = frozenset(
    {"step.started", "step.completed", "clock.read", "approval.awaited"}
)

#: Where the crash harness can exit the process. Each produces a
#: distinguishable journal, which is the point of having four.
CRASH_POINTS: tuple[str, ...] = (
    "after_first_read",
    "after_refund_commit",
    "during_approval_wait",
    "mid_stream",
)

#: Above this a human decides. The same 5,000 cents as everywhere else.
APPROVAL_THRESHOLD_CENTS = 5000


class Suspended(RuntimeError):
    """The run is parked on a human. Not a failure, and not a crash.

    A suspended run is a row in a database and a timer. No worker holds it,
    no container is warm for it, and it costs storage. That is the largest
    cost lever a long-horizon agent has.
    """

    def __init__(self, run_id: str, step_id: str) -> None:
        self.run_id = run_id
        self.step_id = step_id
        super().__init__(f"run {run_id} suspended at {step_id}")


def step_record(
    run_id: str,
    seq: int,
    type: str,
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build one journal record.

    There is no timestamp, and that is not an oversight: a record is
    identified by its sequence number, and replay that depends on the wall
    clock is replay that diverges.

    Raises:
        ValueError: On a type outside :data:`STEP_TYPES`.
    """
    if type not in STEP_TYPES:
        known = ", ".join(sorted(STEP_TYPES))
        raise ValueError(
            f"unknown step type {type!r}; expected one of {known}"
        )
    return {
        "run_id": run_id,
        "seq": seq,
        "type": type,
        "payload": dict(payload or {}),
    }


@dataclass
class RunContext:
    """The handle workflow code holds. Everything effectful goes through it.

    Args:
        run_id: The run's identity. Half of every derived key.
        journal: Append-only record. Defaults to an in-memory one; use
            ``FileJournal`` to survive a restart.
        world: The target system.
        crash_at: A member of :data:`CRASH_POINTS`, or ``None``. Test
            affordance only.
        unsafe_key: Derive idempotency keys from a nonce instead of from
            journaled identity. Reproduces the silent divergence.
        unsafe_clock: Read the wall clock in workflow code. Reproduces the
            other silent divergence.
        strict: Raise :class:`ReplayDivergence` when the workflow asks for
            a step the journal does not have in that position. On during a
            replay test; off during a live run.
    """

    run_id: str
    journal: Journal = field(default_factory=MemoryJournal)
    world: World = field(default_factory=World)
    crash_at: str | None = None
    unsafe_key: bool = False
    unsafe_clock: bool = False
    strict: bool = False

    def __post_init__(self) -> None:
        records = self.journal.records(self.run_id)
        self.seq = len(records)
        #: Completed steps, by step id. Replay serves from here.
        self.completed: dict[str, Any] = {
            r["payload"]["step_id"]: r["payload"]["result"]
            for r in records
            if r["type"] == "step.completed"
        }
        #: Steps whose intent was recorded and whose outcome was not. This
        #: is the interesting set, and it is exactly one entry wide after a
        #: crash in the dangerous window.
        started = [
            r["payload"]["step_id"]
            for r in records
            if r["type"] == "step.started"
        ]
        self.unresolved: list[str] = [
            s for s in started if s not in self.completed
        ]
        #: Journaled clock reads, replayed in order.
        self._clock_reads: list[float] = [
            float(r["payload"]["now"])
            for r in records
            if r["type"] == "clock.read"
        ]
        self._clock_cursor = 0
        self._order: list[str] = started
        self._asked = 0
        #: Steps executed live in this attempt, and steps served from the
        #: record. The demo prints both.
        self.executed: list[str] = []
        self.replayed: list[str] = []

    # ------------------------------------------------------------- identity

    def step_id(self, name: str) -> str:
        """A stable per-step identifier.

        Derived from the name rather than from a counter, so inserting a
        read before a write does not silently renumber the write and mint
        it a new idempotency key.
        """
        return f"{self.run_id}:{name}"

    def key_for(self, name: str) -> str:
        """The idempotency key for one step.

        A pure function of identifiers the journal already holds, so any
        worker replaying the run computes the same key without ever having
        stored it.
        """
        if self.unsafe_key:
            # Broken: a nonce, not a key. The retry presents a new identity
            # for the same intent, and the refund service pays twice.
            import uuid  # noqa: PLC0415 - only the broken path needs it

            return f"{self.run_id}-{uuid.uuid4().hex}"
        return idempotency_key(self.run_id, self.step_id(name))

    # ---------------------------------------------------------------- clock

    def now(self) -> float:
        """A journaled clock. Replay returns the recorded value.

        ``datetime.now()`` returns one value during the original execution
        and a different one during replay, so anything derived from it
        diverges. This returns the same value every time, which is what
        makes a deadline computed in workflow code safe.
        """
        if self.unsafe_clock:
            # Broken: a new value on every replay. Nothing detects it,
            # because the step sequence still matches.
            import time  # noqa: PLC0415 - only the broken path needs it

            return time.time()
        if self._clock_cursor < len(self._clock_reads):
            value = self._clock_reads[self._clock_cursor]
            self._clock_cursor += 1
            return value
        if self.strict:
            raise JournalExhausted(
                f"run {self.run_id} asked for a clock read the journal does "
                f"not hold; the record ends before this point"
            )
        # A deterministic tick rather than the wall clock: the value has to
        # be reproducible on a machine that reads this journal next year.
        value = float(1_785_312_000 + self.seq)
        self._write("clock.read", {"now": value})
        self._clock_reads.append(value)
        self._clock_cursor += 1
        return value

    # ----------------------------------------------------------------- step

    def step(
        self,
        step_id: str,
        fn: Callable[..., Any],
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        """Journal the intent, run the effect, journal the outcome.

        On replay a completed step returns its recorded result and the
        callable is never invoked: the refund does not happen twice because
        the second time it does not happen at all.

        A step whose intent was recorded and whose outcome was not is
        *re-issued* rather than skipped. That is the rule Chapter 8 states
        and engines only half-automate: resolve, do not repeat. The re-issue
        is safe because the key is derived, so the target recognises the
        second attempt as the first one.

        Raises:
            ReplayDivergence: In strict mode, when the workflow asks for a
                step the journal does not hold in this position.
            SimulatedCrash: At the configured crash point.
        """
        position = self._asked
        self._asked += 1
        if step_id in self.completed:
            self.replayed.append(step_id)
            return self.completed[step_id]
        if self.strict:
            recorded = (
                self._order[position] if position < len(self._order) else None
            )
            if recorded is not None and recorded != step_id:
                # The step *sequence* changed. This is the loud class of
                # divergence: the replayed command stream stopped matching
                # the journal, and it fails on the first replay after the
                # offending deploy. Noisy, blocking, and the signal you
                # want.
                raise ReplayDivergence(
                    f"the journal has {recorded!r} at position {position} "
                    f"and the workflow asked for {step_id!r}; the run took "
                    f"a different path"
                )
            # The record ran out, or ran out mid-step. The replay has
            # caught up to reality: this is where the worker went away, and
            # it is the first question in any incident rather than a fault.
            raise JournalExhausted(
                f"run {self.run_id} has no recorded outcome for "
                f"{step_id!r}; the journal ends here"
            )

        self._write("step.started", {"step_id": step_id, "args": _safe(args)})
        result = fn(*args, **kwargs)
        self.executed.append(step_id)
        self._maybe_crash("after_refund_commit", step_id, "issue_refund")
        self._write(
            "step.completed", {"step_id": step_id, "result": _safe(result)}
        )
        self.completed[step_id] = _safe(result)
        self._maybe_crash("after_first_read", step_id, "get_order")
        return result

    def await_approval(self, step_id: str, **payload: Any) -> None:
        """Park the run on a human. Returns nothing; raises to suspend.

        Releasing the worker is the point. A run waiting four hours holds
        no process, no memory, and no connection. It holds a journal.

        Raises:
            Suspended: Unless a decision for this step is already recorded.
            SimulatedCrash: At the ``during_approval_wait`` crash point.
        """
        if step_id in self.completed:
            self.replayed.append(step_id)
            return
        self._write(
            "approval.awaited", {"step_id": step_id, "payload": payload}
        )
        self._maybe_crash("during_approval_wait", step_id, step_id)
        raise Suspended(self.run_id, step_id)

    def record_decision(self, step_id: str, approved: bool, by: str) -> None:
        """Record a human's answer, so the resumed run passes the gate."""
        self._write(
            "step.completed",
            {
                "step_id": step_id,
                "result": {"approved": approved, "by": by},
            },
        )
        self.completed[step_id] = {"approved": approved, "by": by}

    def finish(self, status: str) -> RunState:
        """The run's terminal state, rebuilt from the record."""
        return RunState(
            run_id=self.run_id,
            step=len(self.completed),
            status=status,  # type: ignore[arg-type]
        )

    # ------------------------------------------------------------ internals

    def _write(self, type: str, payload: dict[str, Any]) -> dict[str, Any]:
        """Append one record and advance the sequence."""
        record = step_record(self.run_id, self.seq, type, payload)
        self.seq += 1
        self.journal.append(record)
        return record

    def _maybe_crash(self, point: str, step_id: str, wanted: str) -> None:
        """Exit at the configured point, once the effect has committed."""
        if self.crash_at != point or wanted not in step_id:
            return
        self.crash_at = None
        raise SimulatedCrash(
            f"injected crash at {point} in run {self.run_id} "
            f"(step {step_id})"
        )


def _safe(value: Any) -> Any:
    """Reduce a value to something the journal can hold.

    A tool argument you cannot serialise is a tool argument you cannot
    journal or replay, so this converts what it can and refuses to guess
    about the rest.
    """
    if isinstance(value, dict):
        return {str(k): _safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_safe(v) for v in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return repr(value)


def refund_workflow(
    ctx: RunContext,
    order_id: str,
    amount_cents: int,
) -> RunState:
    """Read, check policy, maybe ask a human, refund, finish.

    Three things in a dozen lines carry the chapter. ``ctx.step`` is the
    workflow-versus-step boundary: nothing outside it touches the world.
    ``ctx.await_approval`` returns rather than blocks, releasing the
    worker. And the key is derived from ``ctx.run_id`` and the journaled
    step identity, so the tenth replay computes the same key as the first.
    """
    # Runs as the support-agent principal, scope refunds.write.
    ctx.step("get_order", ctx.world.get_order, order_id)
    ctx.step("get_policy", ctx.world.get_policy, "damaged")
    deadline = ctx.now() + 72 * 3600      # journaled clock, never wall clock
    if amount_cents >= APPROVAL_THRESHOLD_CENTS:
        ctx.await_approval(
            "refund_approval",
            amount_cents=amount_cents,
            expires_at=deadline,
        )
    key = ctx.key_for("refund")
    ctx.step(
        "issue_refund",
        ctx.world.issue_refund,
        order_id,
        amount_cents,
        "damaged",
        key,
    )
    return ctx.finish("succeeded")
