"""FM-1.5, unawareness of termination conditions: the harness ended the run.

The signature is structural and it is the same whichever way the mode
expresses itself: the run consumed its whole turn allowance and never reached
a terminal state of its own. The run ended in the harness, not in the design.

The mode's mirror image, premature termination (FM-3.1), is the same missing
specification producing the opposite behaviour, and it is *not* detectable
here. A run that stops early looks structurally identical to a run that
finished; only a state grader can tell them apart. That asymmetry is worth
knowing rather than papering over: most of the top modes reduce to a
structural detector, and this one's twin does not.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from catalog import FailureLabel

__all__ = ["detect_termination_unawareness"]


def detect_termination_unawareness(
    run_id: str,
    events: Sequence[dict[str, Any]],
    max_turns: int,
) -> FailureLabel | None:
    """FM-1.5: the turn allowance ran out before the run decided to stop.

    Args:
        run_id: The run these events belong to.
        events: The run's event log.
        max_turns: The ceiling this run was given. The detector needs it
            because "used every turn" is only a finding relative to how
            many turns there were.

    Returns:
        A :class:`~catalog.FailureLabel`, or ``None``.
    """
    turns = [
        int(e["step"]) for e in events if e["type"] == "model.called"
    ]
    finished_well = any(
        e["type"] == "run.finished"
        and e["payload"].get("status") == "succeeded"
        for e in events
    )
    if finished_well or len(turns) < max_turns:
        return None
    return FailureLabel(
        run_id,
        "FM-1.5",
        True,
        (max(turns),),
        "detector",
        f"used all {max_turns} turns and never reached a terminal state; "
        "the run ended in the harness, not in the design",
    )
