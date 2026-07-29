"""The agent loop, written out longhand.

This is the whole idea, with nothing hidden. A model is asked what to do, the
loop does it, the result goes back into the conversation, and the model is
asked again. It stops when the model stops asking for tools.

``northstar_runtime.AgentLoop`` is this loop with the production concerns
attached: checkpointing, policy, approvals, budgets, telemetry, retries. Read
this file first so you know what those concerns are attached *to*.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from northstar_contracts import (
    Message,
    RunState,
    ToolCall,
    ToolError,
    ToolResult,
    ToolSpec,
)
from northstar_runtime import ModelProvider

#: A tool binding: the contract the model sees, and the function that runs.
ToolBinding = tuple[ToolSpec, Callable[..., Any]]


class BudgetExceeded(RuntimeError):
    """The run hit its turn limit before the model chose to stop.

    This is raised, not returned. A loop that quietly truncates and reports
    success is the failure Chapter 1 is about.
    """


class MinimalAgent:
    """A provider-agnostic reason-act-observe loop.

    Args:
        model: Anything with ``complete(messages, tools) -> ModelResponse``.
        tools: ``{name: (spec, fn)}``.
        max_turns: Hard ceiling on model calls. Code owns termination, not
            an instruction in the prompt.
    """

    def __init__(
        self,
        model: ModelProvider,
        tools: dict[str, ToolBinding],
        max_turns: int = 8,
        run_id: str = "run_ch01_demo",
    ) -> None:
        self.model = model
        self.tools = tools
        self.max_turns = max_turns
        self.state = RunState(run_id=run_id)
        self.calls: list[ToolCall] = []

    # -- the loop ---------------------------------------------------------

    def run(self, goal: str) -> RunState:
        """Run until the model stops calling tools, or the budget is gone."""
        self.state = self.state.with_messages(
            Message(role="user", content=goal)
        )

        while self.state.step < self.max_turns:
            self.state = self.state.advance()

            response = self.model.complete(self.state.messages, self.specs())
            self.state = self.state.with_messages(response.as_message())

            # No tool calls means the model considers itself done. The model
            # owns the stopping decision; that is what makes this an agent
            # rather than a workflow.
            if not response.tool_calls:
                self.state = self.state.with_status("succeeded")
                return self.state

            for call in response.tool_calls:
                self.calls.append(call)
                result = self.dispatch(call)
                # Appending the result is the "observe" half of the loop,
                # and the reason context grows with every turn.
                self.state = self.state.with_messages(
                    Message(role="tool", content=self._render(result))
                )

        raise BudgetExceeded(
            f"run {self.state.run_id} hit max_turns={self.max_turns}"
        )

    # -- tool dispatch ----------------------------------------------------

    def dispatch(self, call: ToolCall) -> ToolResult:
        """Run one tool call, turning a failure into an observation.

        A tool that raises does not end the run. The failure is described
        back to the model, which decides what to do about it -- including,
        as Chapter 1 shows, deciding to try again.
        """
        binding = self.tools.get(call.name)
        if binding is None:
            return ToolResult(
                call_id=call.id,
                ok=False,
                content={
                    "error": f"no such tool: {call.name}",
                    "retryable": False,
                },
            )

        _spec, fn = binding
        try:
            value = fn(**call.arguments)
        except ToolError as exc:
            return ToolResult(
                call_id=call.id,
                ok=False,
                content={
                    "error": str(exc),
                    "retryable": getattr(exc, "retryable", False),
                },
            )
        return ToolResult(call_id=call.id, ok=True, content=value)

    # -- helpers ----------------------------------------------------------

    def specs(self) -> list[ToolSpec]:
        """The contracts the model sees this turn."""
        return [spec for spec, _fn in self.tools.values()]

    @staticmethod
    def _render(result: ToolResult) -> dict[str, Any]:
        return {"ok": result.ok, "content": result.content}

    def trajectory(self) -> list[str]:
        """Tool names in the order they were called, for printing."""
        return [call.name for call in self.calls]
