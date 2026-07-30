"""One turn of the harness, with all ten positions visible.

Chapter 1's loop was a while loop that alternated between asking a model
what to do and doing it. That was true and incomplete. Between two model
calls sits the code that assembles messages, publishes tool schemas,
validates arguments, asks permission, dispatches, normalises failures,
counts money, writes state, and decides whether to go around again. That
code is the harness, and almost every production property you care about
lives in it rather than in the model.

:meth:`HarnessLoop.step` is one turn, in order. Two things in it are
load-bearing and easy to get wrong. The budget check happens *before* the
model call rather than after it, so an exhausted run costs nothing extra to
refuse. And the journal records the intent to call a tool before the
dispatch and the evidence after it. A run that dies between those two
writes comes back knowing a call was attempted and not knowing whether it
landed, which is exactly the state that lets you do something sensible
about it.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from budget import BudgetGuard
from checkpoint import MemoryCheckpointer
from journal import StepJournal, step_id_of
from northstar_contracts import (
    Message,
    RunState,
    ToolCall,
    ToolResult,
    ToolSpec,
    idempotency_key,
)
from northstar_runtime import ModelProvider, ModelResponse
from northstar_telemetry import CostLedger
from registry import HarnessRegistry

__all__ = [
    "HarnessLoop",
    "MutableRunState",
    "TurnResponse",
    "WorkerKilled",
    "derived_key",
    "killed_after",
]


class WorkerKilled(BaseException):
    """The process was killed mid-turn.

    A ``BaseException`` on purpose. ``SIGKILL`` has no ``except`` clause,
    and a simulated kill that any ``except Exception`` inside the harness
    could swallow would not be simulating the thing that happens during a
    rolling deploy.
    """


class MutableRunState(RunState):
    """A ``RunState`` the harness advances in place, one turn at a time.

    ``RunState`` is frozen because a run's history is evidence, and evidence
    you can edit is not evidence. The harness needs somewhere to accumulate
    the turn it is in the middle of, so it works on this and hands the
    checkpointer a serialised snapshot. Everything outside the loop — the
    checkpointer, the journal, a grader — still sees a ``RunState``.
    """

    __setattr__ = object.__setattr__    # type: ignore[assignment]
    __delattr__ = object.__delattr__    # type: ignore[assignment]

    @classmethod
    def of(cls, state: RunState) -> MutableRunState:
        """Return a mutable working copy of ``state``."""
        return cls(
            run_id=state.run_id,
            step=state.step,
            messages=list(state.messages),
            status=state.status,
            budget_spent_cents=state.budget_spent_cents,
        )

    def frozen(self) -> RunState:
        """Return an immutable snapshot, for a grader or an assertion."""
        return RunState(
            run_id=self.run_id,
            step=self.step,
            messages=list(self.messages),
            status=self.status,
            budget_spent_cents=self.budget_spent_cents,
        )


def derived_key(run_id: str, step: int, call: ToolCall) -> str:
    """The key a retry of this call must present, from any process."""
    return idempotency_key(run_id, step_id_of(step, call))


class TurnResponse:
    """One model turn, with its content blocks materialised.

    An assistant turn is a list of typed content blocks, not a string: a
    text block explaining what the model is about to do, then one
    ``tool_use`` block per requested call. ``ModelResponse`` keeps text and
    tool calls apart and renders the blocks on demand; the harness wants
    them as an attribute, because the block list is what enters the history.

    ``tool_calls`` here already carry their derived idempotency keys.
    Stamping before the intent is journaled is the point: the journal entry
    has to record the identity a retry will present, or a resumed worker is
    guessing.
    """

    def __init__(
        self,
        response: ModelResponse,
        registry: HarnessRegistry,
        run_id: str,
        step: int,
    ) -> None:
        self.tool_calls: list[ToolCall] = [
            registry.stamp(call, run_id, step_id_of(step, call))
            for call in response.tool_calls
        ]
        self.input_tokens = response.input_tokens
        self.output_tokens = response.output_tokens
        self.model = response.model
        self.content: list[dict[str, Any]] | str = self._blocks(response.text)

    def _blocks(self, text: str | None) -> list[dict[str, Any]] | str:
        """The assistant message body: blocks with tools, plain text without."""
        if not self.tool_calls:
            return text or ""
        blocks: list[dict[str, Any]] = []
        if text:
            blocks.append({"type": "text", "text": text})
        for call in self.tool_calls:
            blocks.append(
                {
                    "type": "tool_use",
                    "id": call.id,
                    "name": call.name,
                    "input": call.arguments,
                }
            )
        return blocks


class _StampingProvider:
    """Wraps a provider so a turn arrives as :class:`TurnResponse`.

    The loop needs the run id and the step number to derive an idempotency
    key, and both live on the loop rather than on the provider, so the
    wrapper reads them back out of it.
    """

    def __init__(self, base: ModelProvider, loop: HarnessLoop) -> None:
        self.base = base
        self.loop = loop

    def complete(
        self,
        messages: list[Message],
        tools: list[ToolSpec],
    ) -> TurnResponse:
        """Take one turn and stamp every write call it asked for."""
        response = self.base.complete(messages, tools)
        self.loop.model_name = response.model
        return TurnResponse(
            response, self.loop.tools, self.loop.state.run_id, self.loop.state.step
        )


def killed_after(
    tools: HarnessRegistry,
    kill_at: Callable[[ToolCall], bool],
) -> HarnessRegistry:
    """A registry that dies once the selected call's effect has landed.

    The kill lands *after* the tool ran and *before* the evidence is
    journaled, which is the only interesting moment. A worker killed before
    the write leaves nothing behind to reconcile, and one killed after the
    evidence is written resumes with no ambiguity at all.

    Subclasses whatever registry it was handed rather than
    :class:`~registry.HarnessRegistry` directly, so wrapping a registry that
    already overrides something — the unkeyed one the wrong-order demo
    uses — does not quietly undo the override.
    """

    class _Killed(type(tools)):    # type: ignore[misc]
        def dispatch(self, call: ToolCall) -> ToolResult:
            """Dispatch, then die if this was the selected call."""
            result = super().dispatch(call)
            if kill_at(call):
                key = call.arguments.get("idempotency_key", "none")
                raise WorkerKilled(
                    f"worker killed after dispatching {call.name} "
                    f"(idempotency_key={key})"
                )
            return result

    killable = _Killed(tools.policy, tools.principal)
    return killable.register_all(tools.bindings())


class HarnessLoop:
    """The Chapter 1 loop with the harness made explicit.

    Args:
        model: Anything with ``complete(messages, tools) -> ModelResponse``.
            Wrapped on the way in, so ``self.model`` yields turns whose
            content blocks and idempotency keys are already in place.
        tools: The dispatch boundary. Validation and policy live inside it.
        checkpointer: Written after every step. Without one the run cannot
            survive the process.
        journal: The append-only record of intents and evidence.
        budget: Turn, money, deadline, and no-progress limits.
        ledger: Cost attribution, priced per model in integer cents.
        system_prompt: The instructions that open the history. Part of the
            configuration hash, which is why a resume compares it.

    Example:
        >>> from northstar_contracts import World
        >>> from northstar_runtime import FakeModel
        >>> world = World()
        >>> loop = HarnessLoop(
        ...     FakeModel(default=["All set."]),
        ...     HarnessRegistry().register_all(world.tools()),
        ... )
        >>> loop.run("check order NR-2026-0041827", "run-doc").status
        'succeeded'
    """

    def __init__(
        self,
        model: ModelProvider,
        tools: HarnessRegistry,
        checkpointer: Any | None = None,
        journal: StepJournal | None = None,
        budget: BudgetGuard | None = None,
        ledger: CostLedger | None = None,
        *,
        system_prompt: str = "You are the Northstar Returns support agent.",
    ) -> None:
        self.tools = tools
        # ``or`` would be a bug on every line here: an empty journal is
        # falsy, because StepJournal defines __len__, and a budget with
        # nothing spent yet could grow the same defect later. Test for
        # ``None`` when a caller might reasonably hand you something empty.
        self.checkpointer = (
            checkpointer if checkpointer is not None else MemoryCheckpointer()
        )
        self.journal = journal if journal is not None else StepJournal("unstarted")
        self.journal.step_of = lambda: self.state.step
        self.budget = (
            budget if budget is not None else BudgetGuard(journal=self.journal)
        )
        self.ledger = ledger if ledger is not None else CostLedger()
        self.system_prompt = system_prompt
        self.model_name = "fake-model-1"
        self.state = MutableRunState(run_id="unstarted")
        self.model: Any = _StampingProvider(model, self)

    # -- entry points -----------------------------------------------------

    def run(self, goal: str, run_id: str) -> RunState:
        """Start a run and drive it until it stops."""
        return self.run_from(self.start(goal, run_id))

    def start(self, goal: str, run_id: str) -> RunState:
        """Build the opening state and checkpoint it before anything runs."""
        self.state = MutableRunState(
            run_id=run_id,
            messages=[
                Message(role="system", content=self.system_prompt),
                Message(role="user", content=goal),
            ],
        )
        self.journal.run_id = run_id
        self.budget.start()
        self.checkpointer.save(self.state)
        return self.state.frozen()

    def run_from(self, state: RunState) -> RunState:
        """Drive an existing state forward until the run stops.

        Safe on a freshly started run and on a state loaded from a
        checkpoint another process wrote. The budget counters come out of
        the state, so a resumed run does not quietly receive a second full
        budget — the bug you get for free if you rebuild the guard and
        forget.
        """
        self.state = MutableRunState.of(state)
        self.journal.run_id = state.run_id
        self.budget.start()
        while self.state.status == "running":
            self.step()
        return self.state.frozen()

    # -- one turn ---------------------------------------------------------

    def step(self) -> RunState:
        self.budget.check(self.state)       # raises BudgetExceeded
        self.state.step += 1
        response = self.model.complete(
            self.state.messages, self.tools.specs()
        )
        self.ledger.record(
            self.model_name, response.input_tokens,
            response.output_tokens
        )
        self.state.messages.append(
            Message(role="assistant", content=response.content)
        )
        for call in response.tool_calls:
            self.journal.append("tool.called", call)   # intent
            result = self.tools.dispatch(call)
            self.state.messages.append(
                Message(role="tool", content=result)
            )
            self.journal.append("tool.result", result) # evidence
        if not response.tool_calls:
            self.state.status = "succeeded"
        self.checkpointer.save(self.state)
        return self.state

    # -- reading a finished run -------------------------------------------

    def trajectory(self) -> list[str]:
        """Tool names in the order they were called, for printing."""
        return self.journal.trajectory(self.state.run_id)

    def spent_cents(self) -> int:
        """What the run cost, from the ledger rather than from the loop."""
        return self.ledger.per_run_cents()
