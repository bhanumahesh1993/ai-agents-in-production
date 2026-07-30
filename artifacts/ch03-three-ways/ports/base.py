"""The port every implementation answers to.

One signature, so the scorecard can drive a hand-written loop, a graph
runtime, and a hosted harness identically and attribute every difference in
the result to the runtime rather than to the driver.
"""

from __future__ import annotations

from typing import Protocol

from northstar_contracts import RunState, ToolSpec
from northstar_runtime import ModelProvider, ToolRegistry

__all__ = ["TriagePort"]


class TriagePort(Protocol):
    """One triage agent, three runtimes, one signature."""

    def build(
        self,
        model: ModelProvider,
        tools: ToolRegistry,
        specs: list[ToolSpec],
    ) -> None: ...

    def run(self, goal: str, run_id: str) -> RunState: ...

    def resume(self, run_id: str) -> RunState: ...
