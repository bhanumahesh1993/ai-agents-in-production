"""The damaged-item task, end to end, on the local stack.

The smoke run exercises the full path rather than the loop in isolation:
the agent reaches its six tools over MCP, policy is evaluated at the
gateway under the support-agent principal, the timeout fault fires on
purpose so it happens on every run rather than one run in a hundred, and
the event log is the run's authoritative history.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import faults
from mcp_server import SUPPORT_PRINCIPAL, MCPServer, registry_for
from model_mode import load_script, mode_from_env, model_for_mode
from northstar_contracts import EventLog, RunState, World
from northstar_runtime import AgentLoop, MemoryCheckpointer, ModelProvider

__all__ = [
    "AMOUNT",
    "GOAL",
    "ORDER",
    "RUN_ID",
    "LocalRun",
    "run_task",
]

ORDER = "NR-2026-0041827"       # US$84.00, delivered, two items
AMOUNT = 3250                   # the lamp shade, in integer cents
RUN_ID = "run_ch21_local"
GOAL = "Customer reports a cracked lamp shade."


@dataclass
class LocalRun:
    """One smoke run, and everything worth asserting on afterwards."""

    world: World
    state: RunState
    server: MCPServer
    events: EventLog
    mode: str

    @property
    def refund_rows(self) -> int:
        """How many refunds landed. The number the demo exits non-zero on."""
        return len(self.world.refunds_for(ORDER))

    @property
    def refunded_cents(self) -> int:
        """What the world holds, not what the agent said."""
        return self.world.total_refunded_cents(ORDER)

    @property
    def model_cents(self) -> int:
        """Model spend. Zero in mock mode, because there was no model."""
        return sum(
            int(r["payload"].get("cost_cents", 0))
            for r in self.events.of_type("model.called")
        )

    def tool_attempts(self, tool: str) -> int:
        """How many times a tool was attempted, successes and failures."""
        return sum(
            1
            for r in self.events.of_type("tool.called")
            if r["payload"]["tool"] == tool
        )

    def trace(self) -> list[str]:
        """The tool timeline, in the shape the chapter prints."""
        lines: list[str] = []
        for record in self.events.of_type("tool.called", "tool.result"):
            payload = record["payload"]
            step = record["step"] + 1
            attempt = int(payload.get("attempt", 1))
            suffix = f"  [attempt {attempt}]" if attempt > 1 else ""
            if record["type"] == "tool.called":
                lines.append(
                    f"worker   {self.state.run_id} step={step}  "
                    f"tool.called   {payload['tool']}{suffix}"
                )
            else:
                detail = "ok" if payload["ok"] else str(payload["error"])
                lines.append(
                    f"worker   {self.state.run_id} step={step}  "
                    f"tool.result   {detail}{suffix}"
                )
        return lines


def run_task(
    *,
    mode: str | None = None,
    model: ModelProvider | None = None,
    inject: str | None = "timeout",
) -> LocalRun:
    """Run the damaged-item task against the local stack.

    Args:
        mode: One of the four model modes. Defaults to ``MODEL_MODE``,
            which defaults to ``mock``.
        model: Provider override, so a test can supply its own script.
        inject: A fault name from :mod:`faults`, or ``None`` for a clean
            run. Defaults to the Chapter 1 timeout, because a failure you
            can reproduce on demand is worth more than one you wait for.

    Returns:
        A :class:`LocalRun`.
    """
    resolved = mode or mode_from_env()
    world = World()
    if inject:
        faults.apply(world, "issue_refund", inject)

    tools = registry_for(world)
    provider = model if model is not None else model_for_mode(resolved)
    loop = AgentLoop(
        model=provider,
        tools=tools,
        checkpointer=MemoryCheckpointer(),
        # No loop-level policy: there is exactly one enforcement point in
        # this stack and it is the gateway. Evaluating twice would let the
        # two drift, and the local one would win.
        policy=None,
        principal=SUPPORT_PRINCIPAL,
        max_turns=8,
    )
    state = loop.run(GOAL, run_id=RUN_ID)
    return LocalRun(
        world=world,
        state=state,
        server=tools.server,
        events=loop.events,
        mode=resolved,
    )


def scripted_steps() -> list[Any]:
    """The hand-written script the mock mode runs, for the demo to print."""
    return load_script("refund.json")
