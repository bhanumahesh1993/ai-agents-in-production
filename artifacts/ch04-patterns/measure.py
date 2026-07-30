"""Price every pattern on the same task, and print the table.

Each pattern builds over a fresh :class:`~northstar_contracts.world.World`,
so no run can see another run's writes, and every number below comes out of
the run that just happened.

One deviation from the excerpt printed in the chapter, and it is
deliberate. The excerpt reads ``model_calls`` off ``state.step`` and sums
``estimate_tokens`` over ``state.messages``. That is right for the plain
loop and wrong for every other row: a router's classification call, a
planner's generation call, a critic's review call, and a search's six
sampling calls all happen outside the loop, so they never appear in the
final ``RunState``. Counting them at the provider is the only way the table
prices what you would actually be billed for.
"""

from __future__ import annotations

import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import task
from critic import build_critic
from northstar_contracts import World
from planner import build_planner
from react import build_react
from router import build_router
from search import build_search
from task import ORDER_ID, TASK, Pattern
from verify import build_verified, verify_refund

__all__ = ["PATTERNS", "PatternCost", "measure", "measure_all", "table"]

#: The six builds, in the order the chapter introduces them. The baseline
#: is first because every other row is a ratio against it.
PATTERNS: dict[str, Callable[[World], Pattern]] = {
    "react": build_react,
    "router": build_router,
    "planner": build_planner,
    "critic": build_critic,
    "verify": build_verified,
    "search": build_search,
}


@dataclass(frozen=True)
class PatternCost:
    name: str
    model_calls: int   # sequential depth; multiply by per-call time
    tokens: int        # estimate_tokens over every message sent
    refund_rows: int   # rows added to the side-effect ledger
    verified: bool     # did the authoritative check pass
    caught: bool = False       # did the pattern itself notice
    status: str = ""           # what the run reported about itself
    notes: tuple[str, ...] = ()


def measure(
    name: str,
    build: Callable[[World], Pattern],
    *,
    fault: bool = False,
) -> PatternCost:
    """Run one pattern on a fresh world and price what it cost.

    Args:
        name: Key in :data:`PATTERNS`.
        build: The pattern's builder.
        fault: Inject the Chapter 1 timeout on ``issue_refund``: the refund
            commits, the response is lost, and the agent retries without an
            idempotency key.
    """
    world = World()
    if fault:
        world.inject_fault("issue_refund", kind="timeout")
    pattern = build(world)
    state = pattern.run(TASK)
    return PatternCost(
        name=pattern.name,
        model_calls=pattern.model_calls,
        tokens=pattern.tokens,
        refund_rows=len(world.effects("refund_issued")),
        verified=verify_refund(world, ORDER_ID, task.AMOUNT_CENTS).ok,
        caught=pattern.caught,
        status=state.status,
        notes=tuple(pattern.notes),
    )


def measure_all(*, fault: bool = False) -> list[PatternCost]:
    """Price every pattern, on the clean fixture or the faulted one."""
    return [
        measure(name, build, fault=fault) for name, build in PATTERNS.items()
    ]


def table(costs: list[PatternCost]) -> None:
    """Print the cost table. The ratio column is against the first row."""
    if not costs:
        return
    base_tokens = costs[0].tokens or 1
    width = max(len(c.name) for c in costs)
    header = (
        f"{'Pattern':<{width}}  {'Calls':>5}  {'Tokens':>7}  "
        f"{'vs base':>8}  {'Rows':>4}  {'Caught':>6}"
    )
    print(header)
    print("-" * len(header))
    for c in costs:
        print(
            f"{c.name:<{width}}  {c.model_calls:>5}  {c.tokens:>7}  "
            f"{c.tokens / base_tokens:>7.2f}x  {c.refund_rows:>4}  "
            f"{('yes' if c.caught else 'no'):>6}"
        )
