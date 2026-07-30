"""Fault injection as infrastructure, not as a testing trick.

In a system whose defining risk is partial failure, the ability to cause a
specific partial failure on demand is infrastructure. Each entry below
corresponds to a real production failure with a *distinct correct
response*, and that is the whole reason to enumerate them rather than
having one generic "make it fail" switch.

Every one is trivially triggerable locally and nearly impossible to trigger
on demand against a real dependency, which is the argument for a fake world
with a fault switch.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from northstar_contracts import FAULT_KINDS, World

__all__ = [
    "FAULTS",
    "Fault",
    "apply",
    "correct_response",
    "unsupported",
]

# artifacts/ch21-local/faults.py (excerpt)
FAULTS: dict[str, dict[str, object]] = {
    "timeout":       {"after_commit": True, "delay_s": 30},
    "slow":          {"delay_s": 8},
    "rate_limited":  {"status": 429, "retry_after_s": 12},
    "expired_token": {"status": 401, "retryable": False},
    "partial":       {"rows_written": 1, "receipt": None},
    "duplicate":     {"replays_last_write": True},
}
# Applied by World.inject_fault(tool, kind=...). Every fault is
# deterministic given the run seed, so a failure reproduces exactly.


@dataclass(frozen=True)
class Fault:
    """One catalogued failure, and what a correct agent does about it."""

    name: str
    settings: dict[str, object]
    #: The kind :meth:`northstar_contracts.World.inject_fault` understands.
    #: ``None`` means the world cannot produce this one, which is a fact
    #: about the fixture rather than about the failure.
    world_kind: str | None
    response: str


#: What each fault demands. These are not interchangeable, and treating
#: them as one class of "error" is how a 401 ends up in a retry loop.
_RESPONSES: dict[str, tuple[str | None, str]] = {
    "timeout": (
        "timeout",
        "The Chapter 1 incident: the write landed and the reply did not. "
        "Answered by a derived idempotency key, never by a blind retry.",
    ),
    "slow": (
        "slow",
        "Answered by a deadline and a budget, so a stuck call ends the run "
        "rather than holding a worker forever.",
    ),
    "rate_limited": (
        "error",
        "Answered by backoff that respects retry_after_s, not by a fixed "
        "sleep and not by giving up.",
    ),
    "expired_token": (
        None,
        "Not retryable at all. Must fail closed and re-acquire, because a "
        "loop around a 401 is a loop that never terminates.",
    ),
    "partial": (
        None,
        "Answered by reconciliation against the side-effect ledger. The "
        "caller cannot tell how much landed, so it has to go and look.",
    ),
    "duplicate": (
        "duplicate",
        "At-least-once delivery replayed the request. A key collapses it; "
        "without one it doubles the money.",
    ),
}


def correct_response(name: str) -> str:
    """What a correct agent does about one fault.

    Raises:
        KeyError: On a fault the catalogue does not name.
    """
    return _RESPONSES[name][1]


def unsupported() -> list[str]:
    """Faults the in-memory world cannot produce.

    Naming them is the point. A catalogue that quietly maps four failures
    onto one fixture behaviour is a catalogue that tells you your agent
    handles cases it has never met. These two need a fixture with a
    multi-row write and a real token boundary, which Chapters 19 and 24
    provide and this one does not.
    """
    return sorted(
        name for name, (kind, _) in _RESPONSES.items() if kind is None
    )


def catalogue() -> list[Fault]:
    """Every fault, with its world mapping and its correct response."""
    return [
        Fault(name, dict(settings), _RESPONSES[name][0], _RESPONSES[name][1])
        for name, settings in FAULTS.items()
    ]


def apply(world: World, tool: str, name: str, times: int = 1) -> Any:
    """Schedule one catalogued fault on one tool.

    Args:
        world: The world to inject into.
        tool: Which tool misbehaves.
        name: A key of :data:`FAULTS`.
        times: How many calls it applies to.

    Returns:
        The scheduled :class:`northstar_contracts.Fault`.

    Raises:
        KeyError: On an unknown fault name.
        NotImplementedError: On a fault this world cannot produce. Saying
            so is better than silently substituting a different failure,
            which would make the suite report coverage it does not have.
    """
    kind = _RESPONSES[name][0]
    if kind is None:
        raise NotImplementedError(
            f"the in-memory world cannot produce {name!r}; it produces "
            f"{', '.join(sorted(FAULT_KINDS))}"
        )
    settings = FAULTS[name]
    return world.inject_fault(
        tool,
        kind=kind,
        times=times,
        # Milliseconds, not the catalogued seconds. The catalogue records
        # what production does; the fixture reproduces the *shape* of the
        # failure, and a test that sleeps eight seconds is a test nobody
        # runs.
        delay_seconds=0.001,
        message=f"{tool} {name} ({settings})",
    )
