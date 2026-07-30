"""What the guard returns, and the six checks a resume runs first.

Kept in its own module because both :mod:`guard` and :mod:`inbox` produce
one of these, and a stored decision that could not answer "may this
proceed" would push the six checks back into the caller, where each caller
would implement five of them.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

__all__ = [
    "RESUME_CHECKS",
    "GuardOutcome",
    "ResumeCheck",
]

#: The six checks a resuming run runs before it executes, in order. The
#: order matters: the cheap checks fail first, so a stale approval costs a
#: dictionary lookup rather than a round trip to the refunds service.
RESUME_CHECKS: tuple[str, ...] = (
    "decision.exists_and_approves",
    "decision.unexpired",
    "approver.still_authorised",
    "policy.still_routes_to_human",
    "versions.unchanged",
    "world.precondition_holds",
)


@dataclass(frozen=True)
class ResumeCheck:
    """One named check, its verdict, and why.

    ``detail`` is what the re-request shows the approver. "Please approve
    this again" without a diff is how you train approvers to click.
    """

    name: str
    ok: bool
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        """JSON-serialisable form, for the inbox's append-only log."""
        return {"name": self.name, "ok": self.ok, "detail": self.detail}


@dataclass(frozen=True)
class GuardOutcome:
    """Proceed with a call, or wait for a human.

    There is deliberately no third value. Denial and budget exhaustion do
    not come back as outcomes: they are raised, because they end the run,
    while an approval requirement is a state the run *lives in*.
    """

    action: Literal["proceed", "wait"]
    fingerprint: str
    reason: str = ""
    checks: tuple[ResumeCheck, ...] = field(default_factory=tuple)

    @classmethod
    def proceed(
        cls,
        fingerprint: str,
        reason: str = "",
        checks: tuple[ResumeCheck, ...] = (),
    ) -> GuardOutcome:
        """The call may be dispatched."""
        return cls("proceed", fingerprint, reason, checks)

    @classmethod
    def wait(
        cls,
        fingerprint: str,
        reason: str = "",
        checks: tuple[ResumeCheck, ...] = (),
    ) -> GuardOutcome:
        """The loop checkpoints, moves to ``waiting_approval``, and stops."""
        return cls("wait", fingerprint, reason, checks)

    @property
    def ok(self) -> bool:
        """Whether the call may proceed."""
        return self.action == "proceed"

    @property
    def failed_checks(self) -> tuple[ResumeCheck, ...]:
        """The checks that refused. This is the diff a re-request shows."""
        return tuple(c for c in self.checks if not c.ok)

    def to_dict(self) -> dict[str, Any]:
        """JSON-serialisable form."""
        return {
            "action": self.action,
            "fingerprint": self.fingerprint,
            "reason": self.reason,
            "checks": [c.to_dict() for c in self.checks],
        }
