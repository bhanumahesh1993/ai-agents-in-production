"""Resuming a run that died in the worst possible place.

Resumability is the property that a run interrupted at any point can
continue on a different process, on a different machine, with a correct
outcome. It is not a deployment feature bolted on later: it constrains where
in the turn you write, which is why it belongs beside the loop.

:meth:`HarnessRunner.resume` is the whole contract. It refuses to continue a
run it cannot identify, refuses to continue one whose configuration has
changed underneath it, rebuilds the turn the dying worker left half
recorded, and re-dispatches the one call whose outcome is unknown — with the
key the first attempt presented, rederived rather than remembered.

At-least-once execution plus a derived idempotency key gives effectively-once
behaviour, which is the strongest guarantee available across a network you do
not control.
"""

from __future__ import annotations

from typing import Any

from checkpoint import SqliteCheckpointer, config_hash_for
from journal import StepJournal
from loop import HarnessLoop
from northstar_contracts import Message, RunState, idempotency_key
from registry import HarnessRegistry

__all__ = ["ConfigDrift", "HarnessRunner", "UnknownRun"]


class UnknownRun(LookupError):
    """There is no checkpoint under this run id.

    Distinct from a run that finished. "I have never heard of this run" and
    "this run is done" call for different operator responses, so they are
    different exceptions.
    """

    def __init__(self, run_id: str) -> None:
        self.run_id = run_id
        super().__init__(f"no checkpoint for run {run_id!r}")


class ConfigDrift(RuntimeError):
    """The run was started under a different effective configuration.

    Resuming under a different system prompt, a different model, or a changed
    tool schema produces a trajectory that half-belongs to each
    configuration, and it is unreproducible by construction. Refusing is the
    correct default. Chapter 25 covers the version-pinning pattern that lets
    in-flight runs finish on the code they started on.
    """

    def __init__(self, run_id: str, expected: str = "", found: str = "") -> None:
        self.run_id = run_id
        self.expected = expected
        self.found = found
        detail = f" (expected {expected!r}, checkpoint says {found!r})"
        super().__init__(
            f"refusing to resume run {run_id!r} under a changed "
            f"configuration{detail if expected else ''}"
        )


class HarnessRunner:
    """Starts runs, and picks up the ones a dead worker left behind.

    Args:
        loop: The loop that will carry the run forward.
        checkpointer: Where state comes from. Must be the durable one for a
            resume to mean anything.
        journal: The append-only record. This, not the checkpoint, is what
            says whether a call was attempted.
        tools: The dispatch boundary, used to settle the pending call before
            the loop takes over.
        config_hash: The configuration this process is running.
    """

    def __init__(
        self,
        loop: HarnessLoop,
        checkpointer: SqliteCheckpointer,
        journal: StepJournal,
        tools: HarnessRegistry,
        config_hash: str,
    ) -> None:
        self.loop = loop
        self.checkpointer = checkpointer
        self.journal = journal
        self.tools = tools
        self.config_hash = config_hash
        self.settled: list[str] = []

    @classmethod
    def build(
        cls,
        model: Any,
        tools: HarnessRegistry,
        db_path: str,
        journal_path: str,
        *,
        system_prompt: str = "You are the Northstar Returns support agent.",
        budget: Any | None = None,
        kill_at: Any | None = None,
    ) -> HarnessRunner:
        """Assemble a runner whose state and journal both live on disk.

        One factory rather than nine lines repeated in the demo and the test,
        and one place where the configuration hash is computed from the same
        inputs in both processes.
        """
        from loop import killed_after  # noqa: PLC0415 - test affordance

        config_hash = config_hash_for(
            model="fake-model-1",
            system_prompt=system_prompt,
            specs=tools.specs(),
        )
        checkpointer = SqliteCheckpointer(db_path, config_hash=config_hash)
        journal = StepJournal.on_file("unstarted", journal_path)
        dispatch_tools = tools if kill_at is None else killed_after(tools, kill_at)
        loop = HarnessLoop(
            model,
            dispatch_tools,
            checkpointer=checkpointer,
            journal=journal,
            budget=budget,
            system_prompt=system_prompt,
        )
        return cls(loop, checkpointer, journal, dispatch_tools, config_hash)

    def start(self, goal: str, run_id: str) -> RunState:
        """Begin a run and drive it until it stops or the worker dies."""
        return self.loop.run_from(self.loop.start(goal, run_id))

    def close(self) -> None:
        """Release the checkpoint database. The journal file needs nothing."""
        self.checkpointer.close()

    def resume(self, run_id: str) -> RunState:
        """Continue a run this process did not start.

        Raises:
            UnknownRun: No checkpoint exists under ``run_id``.
            ConfigDrift: The checkpoint was written under a different
                effective configuration.
        """
        self.journal.run_id = run_id    # this process owns this run now
        state = self.checkpointer.load(run_id)
        if state is None:
            raise UnknownRun(run_id)
        stored_config_hash = self.checkpointer.stored_config_hash
        if self.config_hash != stored_config_hash(run_id):
            raise ConfigDrift(run_id)   # never resume silently
        state = self.journal.replay_decisions(state)
        pending = self.journal.pending_tool_call(run_id)
        if pending is not None:
            # At-least-once: this call may already have landed.
            # The derived key makes a second attempt a lookup.
            key = idempotency_key(run_id, pending.step_id)
            result = self.tools.dispatch(pending.with_key(key))
            state.messages.append(
                Message(role="tool", content=result)
            )
            self.checkpointer.save(state)
            self.settled.append(pending.call.name)
            self.journal.append("tool.result", result, step_id=pending.step_id)
        return self.loop.run_from(state)
