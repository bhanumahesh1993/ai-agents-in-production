"""FM-1.3, step repetition: an identical read, repeated, with nothing new.

The shape every structural detector takes: read the event log, canonicalise,
count. No model, no state, no judgment, and cheap enough to run over every
production trace continuously rather than in a batch job somebody has to
remember to schedule.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from northstar_contracts import World, canonical_json

from catalog import FailureLabel

__all__ = ["TOOL_SPECS", "canonical", "detect_step_repetition"]

#: The tool contracts, by name. ``writes`` is what the detector reads: a
#: repeated *write* is a different and more serious defect that the
#: verification detector owns.
TOOL_SPECS = {spec.name: spec for spec in World().tool_specs()}


def canonical(name: str, arguments: dict[str, Any]) -> str:
    """A stable identity for one call.

    The idempotency key is dropped before hashing. It is derived from the
    run and the step, so two otherwise identical calls always carry
    different keys, and a detector that included it would never fire.
    """
    stripped = {
        k: v for k, v in arguments.items() if k != "idempotency_key"
    }
    return f"{name}:{canonical_json(stripped)}"


def detect_step_repetition(
    run_id: str,
    events: Sequence[dict[str, Any]],
    limit: int = 2,
) -> FailureLabel | None:
    """FM-1.3: identical read call repeated, no new evidence.

    Args:
        run_id: The run these events belong to.
        events: The run's event log.
        limit: How many identical reads are normal. The default is 2
            rather than 1 because one re-acquisition after a compaction
            boundary is ordinary behaviour in a long run, and flagging it
            would bury the real cases. ``nr-run-24`` in the catalog is
            exactly that case, and it is why this detector's
            false-positive rate is not zero.

    Returns:
        A :class:`~catalog.FailureLabel` with the step indices where the
        repetition is visible, or ``None``.
    """
    seen: dict[str, list[int]] = {}
    for event in events:
        if event["type"] != "tool.called":
            continue
        call = event["payload"]
        spec = TOOL_SPECS.get(str(call["tool"]))
        if spec is None or spec.writes:
            continue           # writes are FM-3.x territory
        key = canonical(str(call["tool"]), dict(call.get("arguments", {})))
        seen.setdefault(key, []).append(int(event["step"]))

    for key, steps in seen.items():
        if len(steps) > limit:
            tool = key.split(":", 1)[0]
            return FailureLabel(
                run_id,
                "FM-1.3",
                True,
                tuple(sorted(set(steps))),
                "detector",
                f"identical {tool} repeated {len(steps)} times",
            )
    return None
