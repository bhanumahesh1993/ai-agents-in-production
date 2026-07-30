"""Two writes per consequential call: the intent, then the evidence.

This is the pair of writes that makes the chapter's opening incident
impossible. The old harness appended the model's response, saved, and *then*
dispatched. A run killed in that window came back with the decision on the
record and no record of whether the decision had been carried out, so it
made the call again and seven customers got the same apology twice.

Writing the intent before the side effect and the evidence after it gives a
resumed run three distinguishable situations:

1. no pending call, so continue normally;
2. a pending call with a recorded result, so append the result and continue;
3. a pending call with no result, so the call may or may not have landed.

Only the third is hard, and the derived idempotency key resolves it: the
intent record already carries the key, so re-dispatching is either the first
execution or a lookup of the first execution's receipt.

The checkpoint is not this. A checkpoint is derived, compacted, and replaced
in place; it answers "where is this run now". This log is append-only and
answers "what did this run do". If budget forces you to keep only one, keep
this one.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from northstar_contracts import Message, RunState, ToolCall, ToolResult
from northstar_runtime import FileJournal, Journal, MemoryJournal

__all__ = ["PendingCall", "StepJournal", "step_id_of"]


def step_id_of(step: int, call: ToolCall) -> str:
    """The step identity an idempotency key derives from.

    ``(step, call_id)`` rather than ``step`` alone, because one turn can
    request several parallel calls, and two refunds in one turn are two
    intents rather than one intent retried twice.

    This lives beside the journal because the journal is what has to carry
    it. A resumed worker rederives the key from the intent record, and it
    can only do that if the record says which step the intent belonged to.
    """
    return f"{step}:{call.id}"


@dataclass(frozen=True)
class PendingCall:
    """A tool call whose intent was journaled and whose result was not.

    Args:
        call: The call as it was about to be dispatched, idempotency key
            included. That the key is already here is the point: a resumed
            worker does not have to guess what identity the first attempt
            presented.
        step_id: The step identity the key was derived from, so
            :meth:`with_key` can rederive it and prove it matches.
    """

    call: ToolCall
    step_id: str

    def with_key(self, key: str) -> ToolCall:
        """Return the call carrying ``key`` as its idempotency key.

        Called on resume with a key rederived from the origin run and step.
        For a keyed call this reproduces the argument that is already there,
        which is exactly the property being relied on: the same intent
        presents the same identity, from any process, at any time.
        """
        if "idempotency_key" not in self.call.arguments:
            return self.call
        return ToolCall(
            id=self.call.id,
            name=self.call.name,
            arguments={**self.call.arguments, "idempotency_key": key},
        )


class StepJournal:
    """An append-only record of one run's intents, results, and suspensions.

    Storage is a ``northstar_runtime`` journal: ``MemoryJournal`` for tests,
    ``FileJournal`` for anything that has to survive the process. What this
    class adds is the two-write protocol and the three queries a resumed
    worker needs.

    Args:
        run_id: The run being recorded. The loop sets this when it starts or
            resumes, so one journal object can outlive one run.
        backend: Where records go. Defaults to an in-memory journal, which
            survives an exception and nothing else.
        step_of: Reads the current step number. Used to label an intent with
            the step its idempotency key was derived from.

    Example:
        >>> journal = StepJournal("run-1", step_of=lambda: 1)
        >>> call = ToolCall("c1", "get_order", {"order_id": "NR-2026-0041827"})
        >>> _ = journal.append("tool.called", call)
        >>> pending = journal.pending_tool_call("run-1")
        >>> pending.call.name, pending.step_id
        ('get_order', '1:c1')
    """

    def __init__(
        self,
        run_id: str,
        backend: Journal | None = None,
        *,
        step_of: Callable[[], int] | None = None,
    ) -> None:
        self.run_id = run_id
        self.backend: Journal = (
            backend if backend is not None else MemoryJournal()
        )
        self.step_of: Callable[[], int] = step_of or (lambda: 0)

    @classmethod
    def on_file(
        cls,
        run_id: str,
        path: str | Path,
        *,
        step_of: Callable[[], int] | None = None,
    ) -> StepJournal:
        """A journal that outlives the process, as a JSON Lines file."""
        return cls(run_id, FileJournal(path), step_of=step_of)

    # -- writing ----------------------------------------------------------

    def append(
        self,
        type: str,
        payload: Any = None,
        *,
        step_id: str = "",
        at: float | None = None,
    ) -> dict[str, Any]:
        """Append one record and return it.

        Args:
            type: ``tool.called``, ``tool.result``, ``run.suspended``,
                ``run.resumed``, or anything else your incident review
                wants. Deliberately open: the journal is yours.
            payload: A ``ToolCall``, a ``ToolResult``, or any
                JSON-serialisable value.
            step_id: The step identity an idempotency key derives from.
                Filled in from ``step_of`` for an intent, because the loop
                should not have to say twice which step it is on.
            at: Timestamp, for suspension arithmetic only. Never read
                during replay, because replay that depends on the wall
                clock is replay that diverges.
        """
        if not step_id and isinstance(payload, ToolCall):
            step_id = step_id_of(self.step_of(), payload)
        record: dict[str, Any] = {
            "run_id": self.run_id,
            "seq": len(self.records()),
            "type": type,
            "step_id": step_id,
            "payload": _encode(payload),
        }
        if at is not None:
            record["at"] = at
        self.backend.append(record)
        return record

    # -- reading ----------------------------------------------------------

    def records(self, run_id: str | None = None) -> list[dict[str, Any]]:
        """Every record for this run, in order."""
        return self.backend.records(run_id or self.run_id)

    def pending_tool_call(self, run_id: str) -> PendingCall | None:
        """The call whose intent is recorded and whose result is not.

        Returns:
            The unanswered call, or ``None`` when every intent has an
            outcome. There is at most one: the loop dispatches serially, so
            a second intent cannot be written while the first is open.
        """
        answered = {
            r["payload"].get("call_id")
            for r in self.records(run_id)
            if r["type"] == "tool.result"
        }
        for record in reversed(self.records(run_id)):
            if record["type"] != "tool.called":
                continue
            if record["payload"].get("id") in answered:
                return None
            return PendingCall(
                call=ToolCall.from_dict(record["payload"]),
                step_id=str(record["step_id"]),
            )
        return None

    def recorded_result(self, run_id: str, call_id: str) -> ToolResult | None:
        """The evidence for one call, if the journal has it."""
        for record in self.records(run_id):
            if record["type"] != "tool.result":
                continue
            if record["payload"].get("call_id") == call_id:
                return ToolResult.from_dict(record["payload"])
        return None

    def replay_decisions(self, state: RunState) -> RunState:
        """Rebuild the turn a dying worker left half recorded.

        The checkpoint is written at the end of a turn, so a worker killed
        inside one leaves a checkpoint that predates the model's decision.
        The journal does not: the intent was recorded before the dispatch.
        This is the "message history, *or an event log from which it can be
        rebuilt*" clause of the checkpoint contract, and it is why derived
        history is cheap to lose and the log is not.

        Every journaled intent that the checkpointed history does not
        already contain is appended as the assistant turn that asked for it,
        followed by its result if the journal has one. What remains
        unanswered afterwards is the single call whose outcome nobody knows.

        Returns:
            A state whose history and step count match the journal.
        """
        known = {
            call.id
            for message in state.messages
            for call in message.tool_calls
        }
        messages = list(state.messages)
        for record in self.records(state.run_id):
            if record["type"] != "tool.called":
                continue
            call = ToolCall.from_dict(record["payload"])
            if call.id in known:
                continue
            messages.append(
                Message(
                    role="assistant",
                    content=[
                        {
                            "type": "tool_use",
                            "id": call.id,
                            "name": call.name,
                            "input": call.arguments,
                        }
                    ],
                )
            )
            recorded = self.recorded_result(state.run_id, call.id)
            if recorded is not None:
                messages.append(Message(role="tool", content=recorded))
        turns = sum(1 for m in messages if m.role == "assistant")
        return replace(state, messages=messages, step=turns)

    def suspended_seconds(self, run_id: str) -> float:
        """Total time this run spent suspended rather than working.

        Paired ``run.suspended`` and ``run.resumed`` records. An unpaired
        suspension contributes nothing: the run is still suspended, so no
        interval has closed yet.
        """
        total = 0.0
        opened: float | None = None
        for record in self.records(run_id):
            when = record.get("at")
            if when is None:
                continue
            if record["type"] == "run.suspended":
                opened = float(when)
            elif record["type"] == "run.resumed" and opened is not None:
                total += float(when) - opened
                opened = None
        return total

    def trajectory(self, run_id: str | None = None) -> list[str]:
        """Tool names in the order their intents were journaled."""
        return [
            str(r["payload"].get("name"))
            for r in self.records(run_id)
            if r["type"] == "tool.called"
        ]

    def __len__(self) -> int:
        return len(self.records())


def _encode(payload: Any) -> Any:
    """Render a payload as something a JSON Lines file can hold."""
    if isinstance(payload, ToolCall | ToolResult):
        return payload.to_dict()
    if payload is None:
        return {}
    return payload
