"""Durable execution: journal, replay, resume, cancel, and human waits.

Retrying an agent run from the top is not a recovery strategy. The tool
calls already happened. The money already moved. The customer already got
the email. What you need instead is the durable-execution contract:

* **Journal every non-deterministic result.** Model responses and tool
  results are the two places a rerun can diverge, so both are recorded.
* **Replay from the journal.** On resume, re-execute the loop but serve
  every already-recorded step from the journal instead of re-running it.
  The agent walks the same path back to where it stopped without repeating
  a single side effect.
* **Continue live from the end of the journal.** Once the record runs out,
  the run proceeds normally, appending as it goes.

That is what makes a suspend for a human approval cheap: a run waiting four
hours for someone to click "approve" holds no process, no memory, and no
connection. It holds a journal.

Two things this does *not* fix, and the book says so in Chapter 24. It does
not make a non-idempotent tool safe — replay skips the re-execution, but a
network retry inside the tool is still yours to handle, which is why
:class:`DurableRunner` stamps idempotency keys by default. And it does not
make a run correct: replaying a bad decision faithfully reproduces the bad
decision.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from northstar_contracts import (
    Message,
    Money,
    RunState,
    ToolCall,
    ToolResult,
    ToolSpec,
)
from northstar_policy import ApprovalStore, PolicyEngine, Principal

from .checkpoint import Checkpointer, MemoryCheckpointer
from .loop import AgentLoop, RunCancelled, TelemetrySink
from .providers import ModelProvider, ModelResponse
from .registry import ToolFn, ToolRegistry

__all__ = [
    "JOURNAL_TYPES",
    "DurableRunner",
    "FileJournal",
    "Journal",
    "JournalExhausted",
    "MemoryJournal",
    "ReplayDivergence",
    "SimulatedCrash",
    "journal_record",
]

#: Journal record types. Narrower than the event log on purpose: the event
#: log is for humans reading an incident, the journal is the minimum needed
#: to reconstruct a run exactly.
JOURNAL_TYPES: frozenset[str] = frozenset(
    {
        "run.started",
        "model.response",
        "tool.effect",
        "approval.requested",
        "approval.decided",
        "run.suspended",
        "run.resumed",
        "run.cancelled",
        "run.finished",
    }
)


class SimulatedCrash(RuntimeError):
    """Raised by the test hook that kills a run at an exact step."""


class JournalExhausted(RuntimeError):
    """Replay reached the end of the record and would have gone live."""


class ReplayDivergence(RuntimeError):
    """A replay asked for something the journal does not contain.

    Almost always one of three causes, and worth checking in this order:
    wall-clock time or randomness inside the workflow path, a tool whose
    result is not journaled, or a code change that altered the sequence of
    calls for an in-flight run. The third is why long-running workflows
    need version pinning.
    """


def journal_record(
    run_id: str,
    seq: int,
    type: str,
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build one journal record.

    There is no timestamp. That is not an oversight: a journal entry is
    identified by its sequence number, and replay that depends on wall
    clock is replay that diverges.
    """
    if type not in JOURNAL_TYPES:
        known = ", ".join(sorted(JOURNAL_TYPES))
        raise ValueError(
            f"unknown journal type {type!r}; expected one of {known}"
        )
    return {
        "run_id": run_id,
        "seq": seq,
        "type": type,
        "payload": dict(payload or {}),
    }


@runtime_checkable
class Journal(Protocol):
    """An append-only, ordered record of what a run did."""

    def append(self, record: dict[str, Any]) -> None:
        """Add a record. Never rewrites or removes."""
        ...

    def records(self, run_id: str | None = None) -> list[dict[str, Any]]:
        """Every record, in order, optionally for one run."""
        ...


class MemoryJournal:
    """An in-process journal. Fast, and gone when the process is."""

    def __init__(self) -> None:
        self._records: list[dict[str, Any]] = []

    def append(self, record: dict[str, Any]) -> None:
        """Append one record."""
        self._records.append(dict(record))

    def records(self, run_id: str | None = None) -> list[dict[str, Any]]:
        """Every record, in order, optionally for one run."""
        if run_id is None:
            return [dict(r) for r in self._records]
        return [dict(r) for r in self._records if r["run_id"] == run_id]

    def __len__(self) -> int:
        return len(self._records)


class FileJournal:
    """A JSON Lines journal on disk.

    Append-only in the strictest sense: the file is opened in append mode
    and flushed on every write. Slow, obvious, and hard to corrupt — the
    right trade for a file you will only ever read during an incident.

    Args:
        path: File to append to. Created if missing.
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.touch(exist_ok=True)

    def append(self, record: dict[str, Any]) -> None:
        """Append one record and flush it to the operating system."""
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, sort_keys=True) + "\n")
            handle.flush()

    def records(self, run_id: str | None = None) -> list[dict[str, Any]]:
        """Read every record back, in order."""
        out: list[dict[str, Any]] = []
        with self.path.open("r", encoding="utf-8") as handle:
            for line in handle:
                if not line.strip():
                    continue
                record = json.loads(line)
                if run_id is None or record["run_id"] == run_id:
                    out.append(record)
        return out


class _JournalWriter:
    """Assigns sequence numbers and appends to a journal for one run."""

    def __init__(self, journal: Journal, run_id: str) -> None:
        self.journal = journal
        self.run_id = run_id
        self.seq = len(journal.records(run_id))

    def write(
        self,
        type: str,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Append one record and return it."""
        record = journal_record(self.run_id, self.seq, type, payload)
        self.seq += 1
        self.journal.append(record)
        return record


class _ReplayingModel:
    """Serves journaled model responses, then falls through to the model.

    A model call is the loop's largest source of non-determinism, so it is
    the first thing the journal records. During a replay the base provider
    is never touched: no tokens, no latency, no chance of a different
    answer.
    """

    def __init__(
        self,
        base: ModelProvider,
        recorded: list[ModelResponse],
        writer: _JournalWriter,
        *,
        strict: bool = False,
    ) -> None:
        self.base = base
        self.recorded = recorded
        self.writer = writer
        self.strict = strict
        self.cursor = 0
        self.live_calls = 0

    def complete(
        self,
        messages: list[Message],
        tools: list[ToolSpec],
    ) -> ModelResponse:
        """Return the journaled response, or make a live one and record it."""
        if self.cursor < len(self.recorded):
            response = self.recorded[self.cursor]
            self.cursor += 1
            return response
        if self.strict:
            raise JournalExhausted(
                f"journal holds {len(self.recorded)} model responses; "
                f"the run asked for another"
            )
        response = self.base.complete(messages, tools)
        self.live_calls += 1
        self.writer.write("model.response", response.to_dict())
        return response


class _ReplayingTools(ToolRegistry):
    """A registry that will not re-run an effect the journal already has.

    This is the exactly-once machinery, and it is nine lines of it. If a
    ``(step, call_id)`` pair is in the journal, its recorded result is
    returned and the tool is not called. The refund does not happen twice
    because the second time it does not happen at all.
    """

    def __init__(
        self,
        base: ToolRegistry,
        recorded: dict[tuple[int, str], dict[str, Any]],
        writer: _JournalWriter,
        *,
        strict: bool = False,
    ) -> None:
        super().__init__(
            inject_idempotency_key=base.inject_idempotency_key,
            validate=base.validate,
        )
        self.register_all(base.bindings())
        self.recorded = recorded
        self.writer = writer
        self.strict = strict
        self.replayed = 0
        self.executed = 0

    def dispatch(
        self,
        call: ToolCall,
        run_id: str | None = None,
        step: int | None = None,
    ) -> ToolResult:
        """Replay the recorded result, or execute and record a new one."""
        key = (int(step or 0), call.id)
        recorded = self.recorded.get(key)
        if recorded is not None:
            self.replayed += 1
            return ToolResult.from_dict(recorded["result"])
        if self.strict:
            raise ReplayDivergence(
                f"no journaled result for {call.name} at step {step} "
                f"(call {call.id}); the run took a different path"
            )
        result = super().dispatch(call, run_id=run_id, step=step)
        self.executed += 1
        self.writer.write(
            "tool.effect",
            {
                "step": key[0],
                "call": call.to_dict(),
                "result": result.to_dict(),
            },
        )
        return result


@dataclass(frozen=True)
class _RunSeed:
    """What the journal remembers about how a run was started."""

    goal: str
    system_prompt: str | None


class DurableRunner:
    """Runs an agent with a journal, so a run can outlive its process.

    Args:
        model: The underlying provider. Only called for steps the journal
            does not already have.
        tools: Registry or spec/implementation pairs.
        journal: Where the record goes. Defaults to an in-memory journal;
            use :class:`FileJournal` to survive a restart.
        checkpointer: Optional fast-path state store. The journal is the
            source of truth; the checkpoint is an optimisation.
        policy: Decision point, consulted on replay as well as live.
        approvals: Store used for human waits.
        principal: Identity the run acts as.
        telemetry: Sink for the loop's events.
        max_turns: Turn ceiling per run.
        budget_cents: Money ceiling per run.
        idempotency: Stamp write tools with a key derived from
            ``(run_id, step, call_id)``. On by default. Turning it off is
            how Chapter 1's incident is reproduced.

    Example:
        >>> runner = DurableRunner(model=model, tools=world.tools())
        >>> state = runner.start("refund order NR-2026-0041903")
        >>> state.status
        'succeeded'
    """

    def __init__(
        self,
        model: ModelProvider,
        tools: ToolRegistry | Iterable[tuple[ToolSpec, ToolFn]],
        journal: Journal | None = None,
        checkpointer: Checkpointer | None = None,
        *,
        policy: PolicyEngine | None = None,
        approvals: ApprovalStore | None = None,
        principal: Principal | None = None,
        telemetry: TelemetrySink | None = None,
        max_turns: int = 12,
        budget_cents: Money = 200,
        system_prompt: str | None = None,
        idempotency: bool = True,
    ) -> None:
        self.model = model
        # ``or`` would be a bug here: an empty journal is falsy, because
        # MemoryJournal defines __len__. Test ``is None`` for anything that
        # might reasonably be empty.
        self.journal: Journal = (
            journal if journal is not None else MemoryJournal()
        )
        self.checkpointer: Checkpointer = (
            checkpointer if checkpointer is not None else MemoryCheckpointer()
        )
        self.policy = policy
        self.approvals = approvals
        self.principal = principal or Principal()
        self.telemetry = telemetry
        self.max_turns = max_turns
        self.budget_cents = budget_cents
        self.system_prompt = system_prompt
        self.base_tools = (
            tools
            if isinstance(tools, ToolRegistry)
            else ToolRegistry().register_all(tools)
        )
        self.base_tools.inject_idempotency_key = idempotency
        self.last_loop: AgentLoop | None = None

    # ----------------------------------------------------------------- start

    def start(
        self,
        goal: str,
        run_id: str | None = None,
        *,
        crash_after_step: int | None = None,
    ) -> RunState:
        """Begin a new durable run.

        Args:
            goal: What the agent is being asked to do.
            run_id: Supply one for reproducibility.
            crash_after_step: Raise :class:`SimulatedCrash` once this step
                completes, to prove a resume works. Test affordance only.

        Returns:
            The state the run reached.
        """
        run_id = run_id or f"run-{len(self.journal.records()) + 1:04d}"
        writer = _JournalWriter(self.journal, run_id)
        writer.write(
            "run.started",
            {"goal": goal, "system_prompt": self.system_prompt},
        )
        return self._drive(
            run_id,
            _RunSeed(goal, self.system_prompt),
            writer,
            crash_after_step=crash_after_step,
        )

    def resume(
        self,
        run_id: str,
        *,
        crash_after_step: int | None = None,
    ) -> RunState:
        """Replay a run from its journal and carry on from the end.

        Everything already recorded is served from the journal: no model
        call, no tool execution, no side effect. Once the record runs out,
        the run continues live.

        Raises:
            KeyError: If the journal has never heard of ``run_id``.
            RunCancelled: If the run was cancelled.
        """
        seed = self._seed_for(run_id)
        if self._is_cancelled(run_id):
            raise RunCancelled(run_id, "run was cancelled")
        writer = _JournalWriter(self.journal, run_id)
        writer.write("run.resumed", {})
        return self._drive(
            run_id, seed, writer, crash_after_step=crash_after_step
        )

    def replay(self, run_id: str) -> RunState:
        """Rebuild the run's state from the journal alone.

        Nothing is executed and nothing is recorded. If the journal is
        complete this reproduces the final state exactly; if the run
        crashed, it reproduces the state at the moment of the crash. Use it
        in an incident to answer "what did this agent actually do" without
        doing any of it again.

        Raises:
            ReplayDivergence: If the run needs something not journaled,
                which means the run was not deterministic.
        """
        seed = self._seed_for(run_id)
        writer = _JournalWriter(_NullJournal(), run_id)
        loop = self._build_loop(run_id, writer, strict=True)
        state = loop.start(seed.goal, run_id=run_id)
        # Stepped explicitly rather than through ``resume`` so that a
        # journal which ends mid-run hands back the state at the moment it
        # ended. That state is the answer to "how far did this get before
        # the worker died", which is the first question in any incident.
        while state.status == "running":
            try:
                state = loop.step(state)
            except (JournalExhausted, ReplayDivergence):
                break
        return state

    # ---------------------------------------------------------------- humans

    def approve(
        self,
        run_id: str,
        request_id: str,
        by: str,
        approved: bool = True,
        note: str = "",
    ) -> RunState:
        """Record a human decision and continue the run.

        Raises:
            RuntimeError: If the runner has no approval store.
        """
        if self.approvals is None:
            raise RuntimeError(
                "this runner has no ApprovalStore, so there is nothing to "
                "approve; pass approvals=ApprovalStore() to DurableRunner"
            )
        decision = self.approvals.decide(request_id, approved, by, note)
        writer = _JournalWriter(self.journal, run_id)
        writer.write("approval.decided", decision.to_dict())
        return self.resume(run_id)

    def pending_approvals(self) -> list[Any]:
        """Every approval waiting on a human."""
        return self.approvals.pending() if self.approvals else []

    # ---------------------------------------------------------------- cancel

    def cancel(self, run_id: str, reason: str = "operator cancelled") -> None:
        """Mark a run cancelled. Later resumes refuse to continue it.

        Cancellation is a journal record, not a flag in memory, so it
        survives the same restart the run does. A kill switch that only
        works while the process is alive is not a kill switch.
        """
        writer = _JournalWriter(self.journal, run_id)
        writer.write("run.cancelled", {"reason": reason})
        state = self.checkpointer.load(run_id)
        if state is not None:
            self.checkpointer.save(state.with_status("cancelled"))

    def status(self, run_id: str) -> str:
        """The run's current status, from the checkpoint or the journal."""
        if self._is_cancelled(run_id):
            return "cancelled"
        state = self.checkpointer.load(run_id)
        return state.status if state else "unknown"

    def history(self, run_id: str) -> list[dict[str, Any]]:
        """Every journal record for a run, in order."""
        return self.journal.records(run_id)

    # -------------------------------------------------------------- internal

    def _drive(
        self,
        run_id: str,
        seed: _RunSeed,
        writer: _JournalWriter,
        *,
        crash_after_step: int | None,
    ) -> RunState:
        """Build a replaying loop and run it to its next stopping point."""
        loop = self._build_loop(run_id, writer, strict=False)
        if crash_after_step is not None:
            loop.step_hook = _crash_at(crash_after_step)
        self.last_loop = loop

        state = loop.start(seed.goal, run_id=run_id)
        state = loop.resume(state)

        if state.status == "waiting_approval":
            writer.write(
                "run.suspended",
                {"step": state.step, "reason": "waiting_approval"},
            )
        elif state.is_terminal:
            writer.write(
                "run.finished",
                {
                    "status": state.status,
                    "step": state.step,
                    "budget_spent_cents": state.budget_spent_cents,
                },
            )
        return state

    def _build_loop(
        self,
        run_id: str,
        writer: _JournalWriter,
        *,
        strict: bool,
    ) -> AgentLoop:
        """Assemble a loop whose model and tools replay from the journal."""
        records = self.journal.records(run_id)
        responses = [
            ModelResponse.from_dict(r["payload"])
            for r in records
            if r["type"] == "model.response"
        ]
        effects = {
            (int(r["payload"]["step"]), str(r["payload"]["call"]["id"])): r[
                "payload"
            ]
            for r in records
            if r["type"] == "tool.effect"
        }
        return AgentLoop(
            model=_ReplayingModel(
                self.model, responses, writer, strict=strict
            ),
            tools=_ReplayingTools(
                self.base_tools, effects, writer, strict=strict
            ),
            checkpointer=self.checkpointer,
            policy=self.policy,
            telemetry=self.telemetry,
            max_turns=self.max_turns,
            budget_cents=self.budget_cents,
            principal=self.principal,
            approvals=self.approvals,
            system_prompt=self.system_prompt,
        )

    def _seed_for(self, run_id: str) -> _RunSeed:
        """Recover the goal a run was started with."""
        for record in self.journal.records(run_id):
            if record["type"] == "run.started":
                payload = record["payload"]
                return _RunSeed(
                    goal=str(payload.get("goal", "")),
                    system_prompt=payload.get("system_prompt"),
                )
        raise KeyError(f"no journaled run {run_id!r}")

    def _is_cancelled(self, run_id: str) -> bool:
        """Whether a cancellation record exists for this run."""
        return any(
            r["type"] == "run.cancelled"
            for r in self.journal.records(run_id)
        )


class _NullJournal:
    """A journal that forgets everything, used by :meth:`DurableRunner.replay`."""

    def append(self, record: dict[str, Any]) -> None:
        """Discard the record."""

    def records(self, run_id: str | None = None) -> list[dict[str, Any]]:
        """Always empty."""
        return []


def _crash_at(step: int) -> Callable[[RunState], None]:  # noqa: D401
    """Build a step hook that crashes once the given step has committed."""

    def hook(state: RunState) -> None:
        if state.step >= step and not state.is_terminal:
            raise SimulatedCrash(
                f"simulated worker crash after step {state.step} "
                f"of run {state.run_id}"
            )

    return hook
