"""Human approvals that actually bind.

An approval gate is worth exactly as much as the thing the approval is
attached to. "Approve refund for this customer?" attached to a run id binds
nothing: the agent can be approved for a US$40 refund and then issue US$400,
and the audit trail will show a human said yes.

So an approval here binds a **fingerprint**: the sha256 of the canonical
JSON of the exact call — tool name and every argument. Change the amount by
one cent and the fingerprint changes, the decision no longer applies, and
the gate asks again. That is the property the whole mechanism exists for.

Two consequences worth stating plainly:

* Approvals expire. A yes from four hours ago, on a world that has moved,
  is not consent to act now.
* An approval request must show the human the payload, not a summary. An
  approval flow that renders a paraphrase is approval theatre with a
  cryptographic hash bolted on.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal

from northstar_contracts import ToolCall, content_hash

from .engine import Principal

__all__ = [
    "ApprovalDecision",
    "ApprovalRequest",
    "ApprovalStore",
    "ApprovalStatus",
    "approval_fingerprint",
]

ApprovalStatus = Literal["none", "pending", "approved", "denied", "expired"]

#: How long an approval stays valid unless the caller says otherwise.
DEFAULT_TTL_SECONDS = 3600.0


def approval_fingerprint(call: ToolCall, run_id: str | None = None) -> str:
    """Fingerprint the exact call being approved.

    Args:
        call: The call as it will be dispatched. ``id`` is excluded on
            purpose: the call id changes on a replay, and an approval that
            a replay invalidates is useless in a durable system.
        run_id: Bind the approval to one run as well. Recommended.
            Omit it only when an approval is genuinely meant to be reusable
            across runs, which is rarer than it sounds.

    Returns:
        A 64-character sha256 hex digest.
    """
    payload: dict[str, Any] = {"tool": call.name, "arguments": call.arguments}
    if run_id is not None:
        payload["run_id"] = run_id
    return content_hash(payload)


@dataclass(frozen=True)
class ApprovalRequest:
    """A pending question for a human, and the payload they must see."""

    id: str
    run_id: str
    step: int
    fingerprint: str
    tool: str
    arguments: dict[str, Any]
    reason: str
    requested_at: float
    expires_at: float
    principal: dict[str, Any] | None = None

    def is_expired(self, now: float) -> bool:
        """Whether the request has aged out."""
        return now >= self.expires_at

    def to_dict(self) -> dict[str, Any]:
        """JSON-serialisable form. This is what an inbox renders."""
        return {
            "id": self.id,
            "run_id": self.run_id,
            "step": self.step,
            "fingerprint": self.fingerprint,
            "tool": self.tool,
            "arguments": dict(self.arguments),
            "reason": self.reason,
            "requested_at": self.requested_at,
            "expires_at": self.expires_at,
            "principal": self.principal,
        }


@dataclass(frozen=True)
class ApprovalDecision:
    """A human's answer, bound to one fingerprint."""

    request_id: str
    fingerprint: str
    approved: bool
    by: str
    decided_at: float
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        """JSON-serialisable form."""
        return {
            "request_id": self.request_id,
            "fingerprint": self.fingerprint,
            "approved": self.approved,
            "by": self.by,
            "decided_at": self.decided_at,
            "note": self.note,
        }


class ApprovalStore:
    """In-memory approval inbox with fingerprint binding and expiry.

    Args:
        clock: Injectable time source, so expiry is testable without
            sleeping.
        default_ttl_seconds: How long a request and its decision stay
            valid.

    Example:
        >>> store = ApprovalStore()
        >>> call = ToolCall("c1", "issue_refund", {"amount_cents": 8400})
        >>> req = store.request("run-1", 3, call, reason="over threshold")
        >>> _ = store.decide(req.id, approved=True, by="ops@northstar")
        >>> store.is_approved(call, run_id="run-1")
        True
        >>> bigger = ToolCall("c2", "issue_refund", {"amount_cents": 9900})
        >>> store.is_approved(bigger, run_id="run-1")
        False
    """

    def __init__(
        self,
        clock: Callable[[], float] | None = None,
        default_ttl_seconds: float = DEFAULT_TTL_SECONDS,
    ) -> None:
        self._clock: Callable[[], float] = clock or time.time
        self._ttl = default_ttl_seconds
        self._requests: dict[str, ApprovalRequest] = {}
        self._decisions: dict[str, ApprovalDecision] = {}
        self._counter = 0
        #: Append-only record of every request and decision, in order.
        self.audit: list[dict[str, Any]] = []

    # ------------------------------------------------------------- requests

    def request(
        self,
        run_id: str,
        step: int,
        call: ToolCall,
        *,
        reason: str = "",
        principal: Principal | None = None,
        ttl_seconds: float | None = None,
        bind_run: bool = True,
    ) -> ApprovalRequest:
        """Open an approval request for one call.

        Requesting the same fingerprint twice returns the existing open
        request rather than filling the inbox with duplicates — an agent
        that retries should not generate a second question for a human.
        """
        now = self._clock()
        fingerprint = approval_fingerprint(call, run_id if bind_run else None)
        for existing in self._requests.values():
            if (
                existing.fingerprint == fingerprint
                and not existing.is_expired(now)
                and existing.id not in self._decisions
            ):
                return existing

        self._counter += 1
        request = ApprovalRequest(
            id=f"apr-{self._counter:04d}",
            run_id=run_id,
            step=step,
            fingerprint=fingerprint,
            tool=call.name,
            arguments=dict(call.arguments),
            reason=reason,
            requested_at=now,
            expires_at=now + (ttl_seconds or self._ttl),
            principal=principal.to_dict() if principal else None,
        )
        self._requests[request.id] = request
        self.audit.append({"event": "requested", **request.to_dict()})
        return request

    def decide(
        self,
        request_id: str,
        approved: bool,
        by: str,
        note: str = "",
    ) -> ApprovalDecision:
        """Record a human decision against an open request.

        Raises:
            KeyError: If the request id is unknown.
            ValueError: If the request has already been decided or expired.
                Re-deciding is not allowed: a decision is a fact about a
                moment, and rewriting it destroys the audit trail. Open a
                new request instead.
        """
        request = self._requests.get(request_id)
        if request is None:
            raise KeyError(f"no approval request {request_id!r}")
        if request_id in self._decisions:
            raise ValueError(
                f"{request_id} was already decided; open a new request"
            )
        now = self._clock()
        if request.is_expired(now):
            raise ValueError(f"{request_id} expired at {request.expires_at}")

        decision = ApprovalDecision(
            request_id=request_id,
            fingerprint=request.fingerprint,
            approved=approved,
            by=by,
            decided_at=now,
            note=note,
        )
        self._decisions[request_id] = decision
        self.audit.append({"event": "decided", **decision.to_dict()})
        return decision

    # -------------------------------------------------------------- queries

    def status(
        self,
        call: ToolCall,
        run_id: str | None = None,
        *,
        bind_run: bool = True,
    ) -> ApprovalStatus:
        """The approval status of one exact call.

        Returns ``"none"`` when nothing has ever been asked about this
        fingerprint — which is what a *modified* call returns, even if a
        near-identical call was approved a second ago.
        """
        fingerprint = approval_fingerprint(
            call, run_id if (bind_run and run_id) else None
        )
        now = self._clock()
        latest: ApprovalRequest | None = None
        for request in self._requests.values():
            if request.fingerprint != fingerprint:
                continue
            if latest is None or request.requested_at >= latest.requested_at:
                latest = request
        if latest is None:
            return "none"
        decision = self._decisions.get(latest.id)
        if decision is None:
            return "expired" if latest.is_expired(now) else "pending"
        if latest.is_expired(now):
            return "expired"
        return "approved" if decision.approved else "denied"

    def is_approved(
        self,
        call: ToolCall,
        run_id: str | None = None,
        *,
        bind_run: bool = True,
    ) -> bool:
        """Whether this exact call is currently cleared to run."""
        return self.status(call, run_id, bind_run=bind_run) == "approved"

    def pending(self) -> list[ApprovalRequest]:
        """Undecided, unexpired requests — the human's inbox."""
        now = self._clock()
        return [
            r
            for r in self._requests.values()
            if r.id not in self._decisions and not r.is_expired(now)
        ]

    def get(self, request_id: str) -> ApprovalRequest | None:
        """Look up one request by id."""
        return self._requests.get(request_id)

    def decision_for(self, request_id: str) -> ApprovalDecision | None:
        """Look up the decision on one request, if it has been decided."""
        return self._decisions.get(request_id)
