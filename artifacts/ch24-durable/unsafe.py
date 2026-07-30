"""Both versions of the two most common replay-safety violations.

The rules are easier to believe when you can watch them fail, and the two
failures are not the same kind of failure. That difference matters more
than the rules.

If a divergence changes the **sequence** of steps, the engine detects it:
the replayed command stream stops matching the journal and the run fails
loudly on the first replay after the offending deploy. Noisy, blocking, and
exactly the signal you want.

If a divergence changes only a **value** inside a step's arguments, nothing
detects it. The step sequence still matches, the journal still lines up,
the run continues, and the argument is different from the one recorded. A
timestamp in a message body is the harmless version. An idempotency key
derived from a wall-clock read is the catastrophic one, because it looks
like an idempotency key, passes review, works in every test that does not
crash mid-run, and produces a duplicate write on exactly the path it was
written to protect.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any
from uuid import uuid4

from northstar_contracts import idempotency_key
from workflow import RunContext

__all__ = ["broken_values", "compare", "safe_values"]


def broken_values(ctx: RunContext, order_id: str) -> dict[str, Any]:
    """The unsafe versions. Read the comment, not the code."""
    # artifacts/ch24-durable/unsafe.py (excerpt)
    # Broken: new value on every replay. The engine cannot detect
    # the second line, because the step sequence still matches.
    deadline = datetime.now() + timedelta(hours=72)
    key = f"{order_id}-{uuid4().hex}"
    return {"deadline": deadline.isoformat(), "key": key}


def safe_values(ctx: RunContext, order_id: str) -> dict[str, Any]:
    """The safe versions. Same two lines, different sources."""
    # Safe: journaled clock, key derived from journaled identity.
    deadline = ctx.now() + 72 * 3600
    key = idempotency_key(ctx.run_id, ctx.step_id("refund"))
    return {"deadline": f"{deadline:.0f}", "key": key}


def compare(
    run_id: str,
    order_id: str,
    attempts: int = 2,
) -> dict[str, Any]:
    """Compute both versions twice and report which ones drifted.

    Returns:
        A dict with the values from each attempt and two booleans:
        whether the safe version was stable, and whether the broken one
        was. The second is the interesting one, and it is ``False``.
    """
    safe: list[dict[str, Any]] = []
    broken: list[dict[str, Any]] = []
    journal: Any = None
    for _ in range(attempts):
        ctx = RunContext(run_id=run_id) if journal is None else RunContext(
            run_id=run_id, journal=journal
        )
        journal = ctx.journal
        safe.append(safe_values(ctx, order_id))
        broken.append(broken_values(ctx, order_id))
    return {
        "safe": safe,
        "broken": broken,
        "safe_is_stable": all(entry == safe[0] for entry in safe),
        "broken_is_stable": all(entry == broken[0] for entry in broken),
    }
