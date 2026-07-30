"""The eight states a production run needs, and the legal moves between.

Naming all eight is not bureaucracy. Each answers four operational
questions differently -- is compute held, what is the run waiting for,
what timeout applies, and who can move it -- and the two states teams
usually skip, ``suspended`` and ``resuming``, are the two that make an
incident explicable.

``suspended`` is separate from ``waiting_approval`` because its timeouts
run in minutes rather than days, its escalation is an alert rather than an
inbox, and it resolves on a clock rather than on a decision carrying an
identity. Collapse them and one timeout policy governs both a nine-minute
rate limit and a three-day approval.

``resuming`` is separate from ``running`` because a real resume does work
that can fail before a single step executes: load, version-check, migrate
or refuse, re-evaluate policy, validate the approval, rebuild clients. A
run that dies there should be visible as a failed resume, not as a
mysterious ``running`` run that produced no spans.

Terminal means terminal. Retrying a failed run creates a new run with a new
id linked to the old one; a run record that can leave a terminal state
destroys the property the ledger, the audit log, and the billing pipeline
all depend on.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any

__all__ = [
    "TERMINAL",
    "TRANSITIONS",
    "IllegalTransition",
    "RunPhase",
    "Transition",
    "check_transition",
    "holds_compute",
]


class RunPhase(str, Enum):
    """Where a run is. Subclasses ``str`` so it round-trips through JSON."""

    QUEUED = "queued"
    RUNNING = "running"
    WAITING_APPROVAL = "waiting_approval"
    SUSPENDED = "suspended"
    RESUMING = "resuming"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


#: The three states a run never leaves.
TERMINAL: frozenset[RunPhase] = frozenset(
    {RunPhase.SUCCEEDED, RunPhase.FAILED, RunPhase.CANCELLED}
)

#: Legal moves. The interesting entries are the ones teams discover during
#: an incident rather than during design: lease expiry sending a running
#: run back to the queue, and a stale approval fingerprint sending a
#: resuming run back to ``waiting_approval`` instead of raising.
TRANSITIONS: dict[RunPhase, frozenset[RunPhase]] = {
    RunPhase.QUEUED: frozenset(
        {RunPhase.RUNNING, RunPhase.CANCELLED}
    ),
    RunPhase.RUNNING: frozenset(
        {
            RunPhase.QUEUED,             # lease expired
            RunPhase.WAITING_APPROVAL,
            RunPhase.SUSPENDED,
            RunPhase.SUCCEEDED,
            RunPhase.FAILED,
            RunPhase.CANCELLED,
        }
    ),
    RunPhase.WAITING_APPROVAL: frozenset(
        {
            RunPhase.RESUMING,
            RunPhase.FAILED,             # rejected, or the request expired
            RunPhase.CANCELLED,
        }
    ),
    RunPhase.SUSPENDED: frozenset(
        {RunPhase.RESUMING, RunPhase.FAILED, RunPhase.CANCELLED}
    ),
    RunPhase.RESUMING: frozenset(
        {
            RunPhase.RUNNING,
            RunPhase.WAITING_APPROVAL,   # the approval no longer binds
            RunPhase.FAILED,
            RunPhase.CANCELLED,
        }
    ),
    RunPhase.SUCCEEDED: frozenset(),
    RunPhase.FAILED: frozenset(),
    RunPhase.CANCELLED: frozenset(),
}


class IllegalTransition(RuntimeError):
    """A move the state machine does not allow.

    Raised loudly rather than logged, because a run that reached a state by
    an undeclared path is a run whose history no longer explains it.
    """

    def __init__(self, source: RunPhase, target: RunPhase) -> None:
        self.source = source
        self.target = target
        allowed = ", ".join(sorted(p.value for p in TRANSITIONS[source]))
        super().__init__(
            f"{source.value} -> {target.value} is not a legal transition; "
            f"from {source.value} a run may go to: {allowed or '(nowhere)'}"
        )


@dataclass(frozen=True)
class Transition:
    """One recorded move, with the three fields an audit needs."""

    run_id: str
    source: RunPhase
    target: RunPhase
    at: float
    actor: str
    reason: str

    def to_dict(self) -> dict[str, Any]:
        """JSON-serialisable form, for the transitions table."""
        return {
            "run_id": self.run_id,
            "source": self.source.value,
            "target": self.target.value,
            "at": self.at,
            "actor": self.actor,
            "reason": self.reason,
        }

    def render(self) -> str:
        """One line for an operations console."""
        return (
            f"{self.at:>10.2f}  {self.source.value:<17} -> "
            f"{self.target.value:<17} by {self.actor:<22} {self.reason}"
        )


def check_transition(source: RunPhase, target: RunPhase) -> None:
    """Raise unless ``source -> target`` is declared legal.

    Raises:
        IllegalTransition: Including on any move out of a terminal state.
    """
    if target not in TRANSITIONS[source]:
        raise IllegalTransition(source, target)


def holds_compute(phase: RunPhase) -> bool:
    """Whether a worker lease and model tokens are being consumed.

    ``running`` is the only state that burns tokens, which is why a run
    that spends sixty-one hours in ``waiting_approval`` costs storage and
    nothing else -- provided the pause returned rather than blocking a
    thread.
    """
    return phase in (RunPhase.RUNNING, RunPhase.RESUMING)
