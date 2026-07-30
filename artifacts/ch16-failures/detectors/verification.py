"""FM-3.2, no or incomplete verification: a write commits and nothing checks.

The detector that would have caught Chapter 1. It is deliberately narrow: a
write committed, the run reported success, and nothing read the world
afterwards through an independent path.

Nothing in here mutates anything, so running it twice over the same event log
produces the same label and no side effects. That is what lets it run over
every production trace continuously rather than in a batch job.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from northstar_contracts import RunState

from catalog import FailureLabel

__all__ = ["READ_BACK_TOOLS", "WRITE_TOOLS", "detect_unverified_write"]

#: Writes whose effect somebody has to confirm independently.
WRITE_TOOLS = frozenset({"issue_refund", "send_message"})

#: Reads that count as an independent confirmation. ``get_policy`` does not:
#: it says what the rules are, not what happened.
READ_BACK_TOOLS = frozenset({"get_order", "search_orders"})


def detect_unverified_write(
    run: RunState,
    events: Sequence[dict[str, Any]],
) -> FailureLabel | None:
    """FM-3.2: a write commits, nothing re-reads the world.

    Args:
        run: The finished run. Only its ``status`` and ``run_id`` are
            used, and the status is used to *narrow* the detector rather
            than to grade the run: a run that failed has an obvious
            problem already and does not need this flag on top.
        events: The run's event log.

    Returns:
        A :class:`~catalog.FailureLabel`, or ``None``.
    """
    writes = [
        int(e["step"])
        for e in events
        if e["type"] == "tool.called"
        and e["payload"].get("tool") in WRITE_TOOLS
    ]
    if not writes or run.status != "succeeded":
        return None

    last = max(writes)
    verified = any(
        e["type"] == "tool.result"
        and int(e["step"]) > last
        and e["payload"].get("tool") in READ_BACK_TOOLS
        and e["payload"].get("ok")
        for e in events
    )
    if verified:
        return None
    return FailureLabel(
        run.run_id,
        "FM-3.2",
        True,
        (last,),
        "detector",
        "no post-write read: the run's claim and the ledger were never "
        "compared",
    )
