"""Pattern five: check the system of record, not the run's account of itself.

Verification against authoritative state is not a model call. It reads the
world and compares it to what the run claims. It is nearly free, it is
deterministic, and it is the only pattern in this chapter that catches the
failure the book opens with.

Reflection would not have caught Northstar's double refund. Reflection asks
the agent to review its own account of the run, and the account was the
thing that was wrong: every sentence in that transcript was true, and the
information that two refunds landed is not in it at all.
"""

from __future__ import annotations

import task
from northstar_contracts import Money, RunState, ToolResult, World
from task import Meter, Pattern

__all__ = ["build_verified", "verify_refund"]


def verify_refund(
    world: World, order_id: str, claimed_cents: Money
) -> ToolResult:
    """Check the ledger, not the transcript. No model call."""
    rows = [
        e for e in world.effects("refund_issued")
        if e["order_id"] == order_id
    ]
    total = sum(int(e["amount_cents"]) for e in rows)
    ok = len(rows) == 1 and total == claimed_cents
    return ToolResult(
        call_id="verify",
        ok=ok,
        content={
            "refund_rows": len(rows),
            "total_cents": total,
            "claimed_cents": claimed_cents,
        },
    )


def build_verified(world: World) -> Pattern:
    """The baseline loop, plus one read-only check of the world after it.

    The check asserts two things — the number of refund rows and their
    sum — and both matter. A single row for the wrong amount and two rows
    summing to the right amount are different bugs, and a check that only
    compares totals misses the first.
    """
    meter = Meter()
    loop = task.build_loop(world, meter)
    ref: dict[str, Pattern] = {}

    def run(goal: str) -> RunState:
        result = loop.run(goal, run_id="run_ch04_verified")
        check = verify_refund(world, task.ORDER_ID, task.AMOUNT_CENTS)
        me = ref["pattern"]
        me.caught = not check.ok
        if not check.ok:
            me.notes.append(
                f"{check.content['refund_rows']} refund row(s) totalling "
                f"{check.content['total_cents']}c against a "
                f"{check.content['claimed_cents']}c claim"
            )
            # The run is failed by the check, not by the model. This is the
            # only build in the chapter that ends any other way.
            return result.with_status("failed")
        return result

    pattern = Pattern(name="State verification", meter=meter, runner=run)
    ref["pattern"] = pattern
    return pattern
