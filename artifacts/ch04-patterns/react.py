"""Pattern one: the loop you already have.

ReAct is the baseline every other row is measured against, and it is
Chapter 2's loop with nothing added. What it buys is in-run recovery: a
failed observation is context rather than an exception. What it costs is the
whole accumulated history, re-sent every turn, which is why the token count
is superlinear in turn count and why turn twenty pays for turns one to
nineteen.
"""

from __future__ import annotations

import task
from northstar_contracts import RunState, World
from task import Meter, Pattern

__all__ = ["build_react"]


def build_react(world: World) -> Pattern:
    """The plain loop. No routing, no plan, no critic, no check."""
    meter = Meter()
    loop = task.build_loop(world, meter)

    def run(goal: str) -> RunState:
        return loop.run(goal, run_id="run_ch04_react")

    return Pattern(name="ReAct loop (baseline)", meter=meter, runner=run)
