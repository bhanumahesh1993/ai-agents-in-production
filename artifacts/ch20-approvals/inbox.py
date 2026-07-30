"""The task inbox: where a run that pauses for a human waits.

A run that pauses may wait eleven hours. This is the half most teams
underbuild, and it has four outcomes rather than two.

**Approve** binds the decision to the fingerprint and returns the run to
``running``. **Reject** carries a reason the agent receives as data, so the
run can take a different branch instead of learning nothing. **Correct** is
the outcome most systems omit and most approvers want: the specialist edits
24,000 to 8,400, which is a *different call*, so it produces a different
fingerprint, and it is recorded as a modification attributed to the person
who made it. **Escalate** moves the request up a ladder that never widens
authority — "nobody responded, so proceed" is the absence of a control with
a timer attached.

:class:`TaskInbox` subclasses :class:`northstar_policy.ApprovalStore`
rather than wrapping it, for one reason worth stating: the agent loop calls
``approvals.is_approved(call, run_id)`` and ``approvals.request(...)``, so a
subclass drops straight into the real runtime and the fingerprint binding
gets exercised by the loop instead of by a fixture. What the subclass
changes is *what gets fingerprinted*: the bound call from :mod:`fingerprint`,
carrying the canonicalization version, the tool version, and the principal,
rather than the bare call.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from northstar_contracts import ToolCall
from northstar_policy import (
    ApprovalDecision,
    ApprovalRequest,
    ApprovalStatus,
    ApprovalStore,
    Principal,
)

from fingerprint import ToolVersions, bind, fingerprint
from outcomes import RESUME_CHECKS, GuardOutcome, ResumeCheck

__all__ = [
    "ESCALATION_LADDER",
    "ApprovalRecord",
    "ResumeState",
    "TaskInbox",
]

#: Northstar's ladder: four hours to the rotation, four more to the duty
#: manager, expiry to rejection at twelve. Escalation moves the request; it
#: never grants the action.
ESCALATION_LADDER: tuple[tuple[float, str], ...] = (
    (4 * 3600.0, "rota:fraud-review"),
    (8 * 3600.0, "role:duty-manager"),
    (12 * 3600.0, "reject"),
)


@dataclass(frozen=True)
class ResumeState:
    """The facts a resume checks the stored decision against.

    Every field is something the runtime holds independently. None of it
    comes from the transcript, the model's justification, or the payload
    the approver read, because all three are things an attacker can write.
    """

    tool_version: str
    policy_version: str
    world_version: str
    authorised: frozenset[str] = frozenset()
    still_routes_to_human: bool = True


@dataclass(frozen=True)
class ApprovalRecord:
    """One request, its decision if it has one, and its provenance.

    Args:
        request: The stored request, holding the fingerprint.
        decision: The human's answer, or ``None`` while pending.
        payload: What the approver actually saw.
        authority: The role the decision was made under. People change
            teams, which is the third resume check.
        issued_against: The tool version, policy version, and world version
            current when the request was raised.
        consumed_at_steps: Steps that have already used this decision.
    """

    request: ApprovalRequest
    decision: ApprovalDecision | None
    payload: dict[str, Any] = field(default_factory=dict)
    authority: str = ""
    issued_against: ResumeState | None = None
    consumed_at_steps: frozenset[int] = frozenset()

    @property
    def fingerprint(self) -> str:
        """What this decision binds to. One call, in one run."""
        return self.request.fingerprint

    @property
    def approved(self) -> bool:
        """Whether a human said yes."""
        return self.decision is not None and self.decision.approved

    def check(
        self,
        fingerprint: str,
        step: int,
        now: float,
        current: ResumeState,
    ) -> GuardOutcome:
        """Run the six resume checks and return proceed or wait.

        The order is the order in :data:`outcomes.RESUME_CHECKS`, and it is
        deliberate: the cheap checks fail first, so a stale approval costs a
        dictionary lookup rather than a round trip to the refunds service.

        A failing check returns the run to ``waiting_approval`` with the
        failed checks attached, never to ``failed``. The re-request states
        what changed, because "please approve this again" without a diff is
        how you train approvers to click.
        """
        issued = self.issued_against or current
        checks = (
            ResumeCheck(
                RESUME_CHECKS[0],
                self.fingerprint == fingerprint and self.approved,
                ""
                if self.fingerprint == fingerprint
                else "the call changed since it was approved",
            ),
            ResumeCheck(
                RESUME_CHECKS[1],
                not self.request.is_expired(now),
                f"expired at {self.request.expires_at:g}",
            ),
            ResumeCheck(
                RESUME_CHECKS[2],
                not self.authority or self.authority in current.authorised,
                f"{self.authority} no longer holds this authority",
            ),
            ResumeCheck(
                RESUME_CHECKS[3],
                current.still_routes_to_human
                and issued.policy_version == current.policy_version,
                f"policy moved from {issued.policy_version} to "
                f"{current.policy_version}",
            ),
            ResumeCheck(
                RESUME_CHECKS[4],
                issued.tool_version == current.tool_version,
                f"tool version moved from {issued.tool_version} to "
                f"{current.tool_version}",
            ),
            ResumeCheck(
                RESUME_CHECKS[5],
                issued.world_version == current.world_version,
                f"world moved from {issued.world_version} to "
                f"{current.world_version}",
            ),
        )
        failed = [c for c in checks if not c.ok]
        if failed:
            return GuardOutcome.wait(
                fingerprint,
                "; ".join(c.detail or c.name for c in failed),
                checks,
            )
        return GuardOutcome.proceed(fingerprint, "approved", checks)


class TaskInbox(ApprovalStore):
    """A file-backed inbox with fingerprint binding and four outcomes.

    Args:
        principal: Who the runs act as. Part of every fingerprint.
        tool_versions: Declared tool versions. Part of every fingerprint.
        path: JSON Lines file the inbox appends to. ``None`` keeps the log
            in memory, which is what the tests use.
        clock: Injectable time source, so expiry is testable without
            sleeping.
        default_ttl_seconds: How long a request stays open. Twelve hours,
            matching the bottom rung of :data:`ESCALATION_LADDER`.
        route_to: The role a request is addressed to. Never a person: a
            request addressed to a named specialist expires when that
            specialist takes a Thursday off.
    """

    def __init__(
        self,
        principal: Principal,
        tool_versions: ToolVersions,
        path: str | Path | None = None,
        clock: Callable[[], float] | None = None,
        default_ttl_seconds: float = 12 * 3600.0,
        route_to: str = "rota:fraud-review",
    ) -> None:
        super().__init__(clock=clock, default_ttl_seconds=default_ttl_seconds)
        self.principal = principal
        self.tool_versions = tool_versions
        self.route_to = route_to
        self.path = Path(path) if path is not None else None
        if self.path is not None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.touch(exist_ok=True)
        self._by_fingerprint: dict[str, str] = {}
        self._payloads: dict[str, dict[str, Any]] = {}
        self._authority: dict[str, str] = {}
        self._issued_against: dict[str, ResumeState] = {}
        self._consumed: dict[str, set[int]] = {}
        #: Every event, in order. Append-only: a decision that can be
        #: edited afterwards proves nothing, which is why a correction is a
        #: new request rather than an edit of the old one.
        self.events: list[dict[str, Any]] = []

    # ------------------------------------------------------- fingerprinting

    def bind_call(self, call: ToolCall) -> ToolCall:
        """Wrap a call with the envelope that belongs under the hash."""
        return bind(
            call, self.principal, self.tool_versions.version(call.name)
        )

    def fingerprint_for(self, call: ToolCall, run_id: str) -> str:
        """The fingerprint this inbox binds decisions to."""
        return fingerprint(
            call,
            self.principal,
            run_id,
            self.tool_versions.version(call.name),
        )

    # --------------------------------------- the ApprovalStore overrides

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
        """Open a request against the *bound* call.

        The agent loop calls this with a bare call. Binding here rather
        than at the call site is what makes the loop's own approval path
        carry the tool version and the principal without knowing it does.
        """
        stored = super().request(
            run_id,
            step,
            self.bind_call(call),
            reason=reason,
            principal=principal or self.principal,
            ttl_seconds=ttl_seconds,
            bind_run=bind_run,
        )
        is_new = stored.id not in self._authority
        self._by_fingerprint[stored.fingerprint] = stored.id
        self._authority.setdefault(stored.id, self.route_to)
        if not is_new:
            # Requesting the same fingerprint twice returns the open
            # request rather than filling a human's queue with duplicates.
            return stored
        self._emit(
            "requested",
            {
                "request_id": stored.id,
                "fingerprint": stored.fingerprint,
                "run_id": run_id,
                "step": step,
                "tool": call.name,
                "arguments": dict(call.arguments),
                "reason": reason,
                "routed_to": self.route_to,
                "expires_at": stored.expires_at,
                "on_expiry": "reject",
            },
        )
        return stored

    def status(
        self,
        call: ToolCall,
        run_id: str | None = None,
        *,
        bind_run: bool = True,
    ) -> ApprovalStatus:
        """The approval status of one exact call, bound the same way."""
        return super().status(
            self.bind_call(call), run_id, bind_run=bind_run
        )

    # ------------------------------------------------------------- payloads

    def attach(
        self,
        request_id: str,
        payload: dict[str, Any],
        issued_against: ResumeState,
    ) -> None:
        """Record what the approver saw and what it was issued against."""
        self._payloads[request_id] = dict(payload)
        self._issued_against[request_id] = issued_against

    def payload_for(self, request_id: str) -> dict[str, Any]:
        """The payload a reviewer opened, or an empty dict."""
        return dict(self._payloads.get(request_id, {}))

    def find(self, fingerprint: str) -> ApprovalRecord | None:
        """The record bound to one fingerprint, or ``None``.

        ``None`` is what a *modified* call gets, even when a near-identical
        call was approved a second ago. That is the mechanism.
        """
        request_id = self._by_fingerprint.get(fingerprint)
        if request_id is None:
            return None
        request = self.get(request_id)
        if request is None:
            return None
        return ApprovalRecord(
            request=request,
            decision=self.decision_for(request_id),
            payload=self.payload_for(request_id),
            authority=self._authority.get(request_id, ""),
            issued_against=self._issued_against.get(request_id),
            consumed_at_steps=frozenset(self._consumed.get(fingerprint, ())),
        )

    # ------------------------------------------------------- four outcomes

    def approve(
        self,
        request_id: str,
        by: str,
        note: str = "",
    ) -> ApprovalDecision:
        """Bind the decision to the fingerprint and let the run continue."""
        decision = self.decide(request_id, True, by, note)
        self._emit("approved", decision.to_dict())
        return decision

    def reject(
        self,
        request_id: str,
        by: str,
        reason: str,
    ) -> ApprovalDecision:
        """Refuse, with a reason the agent receives as data.

        A bare denial teaches the agent nothing. "Policy requires the
        return to be received first" lets the run take a different branch
        and gives the evaluation set a labelled example.
        """
        decision = self.decide(request_id, False, by, reason)
        self._emit("rejected", decision.to_dict())
        return decision

    def correct(
        self,
        request_id: str,
        by: str,
        arguments: dict[str, Any],
        note: str = "",
    ) -> tuple[ApprovalRequest, ApprovalDecision]:
        """Edit the call, which produces a different fingerprint.

        The original is rejected as superseded and a new request is opened
        for the corrected call, approved by the person who made the
        modification. It is recorded as a modification rather than as an
        approval, because modification rate is one of the more useful
        signals an approval system produces.

        Raises:
            KeyError: If the request id is unknown.
        """
        original = self.get(request_id)
        if original is None:
            raise KeyError(f"no approval request {request_id!r}")
        self.reject(request_id, by, note or "superseded by correction")

        inner = original.arguments.get("arguments", {})
        corrected = ToolCall(
            id=f"{request_id}-corrected",
            name=original.tool,
            arguments={**inner, **arguments},
        )
        new_request = self.request(
            original.run_id,
            original.step,
            corrected,
            reason=f"corrected by {by}: {note}".strip(": "),
        )
        self._authority[new_request.id] = by
        self._issued_against[new_request.id] = self._issued_against.get(
            request_id, ResumeState("", "", "")
        )
        decision = self.approve(new_request.id, by, note)
        self._emit(
            "corrected",
            {
                "from_request": request_id,
                "to_request": new_request.id,
                "by": by,
                "was": dict(inner),
                "now": dict(corrected.arguments),
                "fingerprint": new_request.fingerprint,
            },
        )
        return new_request, decision

    def escalate(self, request_id: str, now: float | None = None) -> str:
        """Move the request up the ladder. Never widens authority.

        Returns:
            The rung the request is now on. ``"reject"`` is the bottom of
            the ladder and means the request has been refused by timeout.
        """
        request = self.get(request_id)
        if request is None:
            raise KeyError(f"no approval request {request_id!r}")
        elapsed = (now if now is not None else self._clock()) - (
            request.requested_at
        )
        rung = self._authority.get(request_id, self.route_to)
        for after, target in ESCALATION_LADDER:
            if elapsed >= after:
                rung = target
        if rung == "reject":
            # Nothing is decided here, deliberately. An expired request
            # cannot be decided at all -- ``ApprovalStore.decide`` refuses
            # it -- and that is the mechanism rather than a limitation:
            # ``status`` answers "expired", ``is_approved`` answers False,
            # and the run re-requests. Any path where "nobody responded"
            # results in the action proceeding is a control with a delay in
            # it rather than a control.
            self._emit(
                "expired",
                {
                    "request_id": request_id,
                    "fingerprint": request.fingerprint,
                    "on_expiry": "reject",
                },
            )
            return "reject"
        self._authority[request_id] = rung
        self._emit(
            "escalated",
            {"request_id": request_id, "to": rung, "elapsed_s": elapsed},
        )
        return rung

    # --------------------------------------------------------- single use

    def consume(self, fingerprint: str, step: int) -> bool:
        """Record that a step used this decision. Idempotent per step.

        A replay of the recorded step reuses the decision; the same call
        arriving at a *later* step is a new request. That is why the step
        is not in the fingerprint and is recorded here instead.

        Returns:
            ``True`` if this step may use the decision.
        """
        used = self._consumed.setdefault(fingerprint, set())
        if step in used:
            return True  # a replay of the recorded step
        if used:
            return False  # a different step, so a new request is needed
        used.add(step)
        self._emit("consumed", {"fingerprint": fingerprint, "step": step})
        return True

    # --------------------------------------------------------- rendering

    def notification(self, request_id: str) -> str:
        """One line an approver can triage without opening anything.

        "You have 1 pending approval" forces a context switch to find out
        whether it matters. This does not.
        """
        request = self.get(request_id)
        if request is None:
            return f"unknown request {request_id}"
        inner = request.arguments.get("arguments", {})
        remaining = max(0.0, request.expires_at - self._clock()) / 3600.0
        flags = self.payload_for(request_id).get("preview", {}).get("flags")
        return (
            f"{inner.get('amount_cents')} cents, "
            f"order {inner.get('order_id')}, "
            f"{'flagged ' + ','.join(flags) if flags else 'no flags'}, "
            f"expires in {remaining:.0f}h"
        )

    # ---------------------------------------------------------- internals

    def _emit(self, event: str, payload: dict[str, Any]) -> None:
        """Append one event to the log, and to the file if there is one."""
        record = {"event": event, "at": self._clock(), **payload}
        self.events.append(record)
        if self.path is not None:
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, sort_keys=True) + "\n")

    def events_of(self, *kinds: str) -> list[dict[str, Any]]:
        """Every logged event of the given kinds, in order."""
        wanted = set(kinds)
        return [e for e in self.events if e["event"] in wanted]

    def replay_file(self) -> list[dict[str, Any]]:
        """Read the file back. An inbox nobody can audit is a queue."""
        if self.path is None:
            return list(self.events)
        return [
            json.loads(line)
            for line in self.path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
