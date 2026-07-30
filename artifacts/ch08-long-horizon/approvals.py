"""Approval requests and decisions that outlive the worker that asked.

``northstar_policy.ApprovalStore`` holds its inbox in a dict, which is the
right shape for a chapter where the request and the decision happen inside
one process. This chapter's run waits sixty-one hours across a weekend and
three separate processes, so the inbox has to be in the same file as the
checkpoint.

What is *not* reimplemented here is the part that matters:
:func:`northstar_policy.approval_fingerprint` is imported unchanged. An
approval binds the canonical JSON of one exact call, so changing the amount
by one cent -- or adding a field to the payload during a deploy -- means
the decision no longer applies and the run has to ask again. That property
is the whole mechanism, and reimplementing it here would be a second
chance to get it wrong.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from northstar_contracts import ToolCall
from northstar_policy import Principal, approval_fingerprint

__all__ = ["DurableApprovals", "PendingApproval"]

#: How long an approval request stays open. A request with no deadline is a
#: standing grant against a world that keeps changing.
DEFAULT_TTL_SECONDS = 72 * 3600.0

_SCHEMA = """
CREATE TABLE IF NOT EXISTS approval_requests (
    id           TEXT PRIMARY KEY,
    run_id       TEXT NOT NULL,
    step_id      INTEGER NOT NULL,
    fingerprint  TEXT NOT NULL,
    tool         TEXT NOT NULL,
    arguments    TEXT NOT NULL,
    reason       TEXT NOT NULL,
    requested_at REAL NOT NULL,
    expires_at   REAL NOT NULL,
    principal    TEXT NOT NULL,
    queue        TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS approval_decisions (
    request_id  TEXT PRIMARY KEY,
    fingerprint TEXT NOT NULL,
    approved    INTEGER NOT NULL,
    by          TEXT NOT NULL,
    decided_at  REAL NOT NULL,
    note        TEXT NOT NULL
);
"""


class PendingApproval:
    """One open request, and the decision on it if there is one."""

    def __init__(
        self,
        row: sqlite3.Row,
        decision: sqlite3.Row | None,
    ) -> None:
        self.id = str(row["id"])
        self.run_id = str(row["run_id"])
        self.step_id = int(row["step_id"])
        self.fingerprint = str(row["fingerprint"])
        self.tool = str(row["tool"])
        self.arguments: dict[str, Any] = json.loads(str(row["arguments"]))
        self.reason = str(row["reason"])
        self.requested_at = float(row["requested_at"])
        self.expires_at = float(row["expires_at"])
        self.queue = str(row["queue"])
        self.decided = decision is not None
        self.approved = bool(decision["approved"]) if decision else False
        self.decided_by = str(decision["by"]) if decision else ""

    @property
    def call(self) -> ToolCall:
        """The call as it was fingerprinted, rebuilt for comparison."""
        return ToolCall(f"call-{self.id}", self.tool, dict(self.arguments))

    def is_expired(self, now: float) -> bool:
        """Whether the request aged out. Expiry is a transition."""
        return now >= self.expires_at

    def render(self) -> str:
        """One line for the operations console."""
        state = (
            ("approved" if self.approved else "rejected")
            if self.decided
            else "pending"
        )
        return (
            f"{self.id} {self.tool} {self.arguments} -> {state}"
            f"{' by ' + self.decided_by if self.decided_by else ''}"
        )


class DurableApprovals:
    """A file-backed approval inbox, addressed to a queue and not a person.

    The opening incident cost sixty-one hours because a request was
    addressed to someone on leave. That is a routing problem with a known
    solution, and it is solvable precisely because the wait is a state
    something else can observe -- so ``queue`` is a required field here
    rather than an optional one.

    Args:
        path: SQLite file, shared with the envelope store and the ledger.
        clock: Injectable time source.
    """

    def __init__(self, path: str | Path, clock: Any | None = None) -> None:
        self.path = str(path)
        self._db = sqlite3.connect(self.path)
        self._db.row_factory = sqlite3.Row
        self._db.executescript(_SCHEMA)
        self._db.commit()
        self._clock = clock or _ticks(self._last_recorded())

    def request(
        self,
        run_id: str,
        step_id: int,
        call: ToolCall,
        *,
        reason: str,
        principal: Principal,
        queue: str = "fraud-review",
        ttl_seconds: float = DEFAULT_TTL_SECONDS,
    ) -> PendingApproval:
        """Open a request bound to the fingerprint of this exact call.

        Asking twice for the same fingerprint returns the open request
        rather than filling the inbox: an agent that retries should not
        generate a second question for a human.
        """
        now = self._clock()
        fingerprint = approval_fingerprint(call, run_id)
        existing = self._by_fingerprint(fingerprint)
        if existing is not None and not existing.is_expired(now):
            return existing

        # The fingerprint is part of the id, not just of the row. A
        # re-request after a call changed has to be a *different* request,
        # or the decision row still keyed to the old id attaches itself to
        # the new question and a stale approval silently applies.
        request_id = f"apr-{run_id[-6:]}-{step_id:03d}-{fingerprint[:8]}"
        self._db.execute(
            "INSERT OR REPLACE INTO approval_requests (id, run_id, step_id, "
            " fingerprint, tool, arguments, reason, requested_at, "
            " expires_at, principal, queue) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                request_id,
                run_id,
                step_id,
                fingerprint,
                call.name,
                json.dumps(call.arguments, sort_keys=True),
                reason,
                now,
                now + ttl_seconds,
                json.dumps(principal.to_dict(), sort_keys=True),
                queue,
            ),
        )
        self._db.commit()
        found = self.get(request_id)
        assert found is not None  # noqa: S101 - just written, in this txn
        return found

    def decide(
        self,
        request_id: str,
        approved: bool,
        by: str,
        note: str = "",
    ) -> PendingApproval:
        """Record a human's answer against one open request.

        Raises:
            KeyError: On an unknown request id.
            ValueError: If the request was already decided or has expired.
                A decision is a fact about a moment; rewriting it destroys
                the audit trail. Open a new request instead.
        """
        request = self.get(request_id)
        if request is None:
            raise KeyError(f"no approval request {request_id!r}")
        if request.decided:
            raise ValueError(
                f"{request_id} was already decided; open a new request"
            )
        now = self._clock()
        if request.is_expired(now):
            raise ValueError(f"{request_id} expired at {request.expires_at}")
        self._db.execute(
            "INSERT INTO approval_decisions (request_id, fingerprint, "
            " approved, by, decided_at, note) VALUES (?, ?, ?, ?, ?, ?)",
            (request_id, request.fingerprint, int(approved), by, now, note),
        )
        self._db.commit()
        decided = self.get(request_id)
        assert decided is not None  # noqa: S101 - just written
        return decided

    def pending(self, run_id: str) -> PendingApproval | None:
        """The newest request for a run, decided or not.

        Ties on ``requested_at`` are broken by id so that two processes
        writing in the same tick still produce one deterministic answer.
        """
        row = self._db.execute(
            "SELECT * FROM approval_requests WHERE run_id = ? "
            "ORDER BY requested_at DESC, id DESC LIMIT 1",
            (run_id,),
        ).fetchone()
        return self._with_decision(row) if row else None

    def get(self, request_id: str) -> PendingApproval | None:
        """One request by id."""
        row = self._db.execute(
            "SELECT * FROM approval_requests WHERE id = ?", (request_id,)
        ).fetchone()
        return self._with_decision(row) if row else None

    def inbox(self, queue: str) -> list[PendingApproval]:
        """Undecided, unexpired requests on one queue."""
        rows = self._db.execute(
            "SELECT * FROM approval_requests WHERE queue = ? "
            "ORDER BY requested_at",
            (queue,),
        ).fetchall()
        now = self._clock()
        return [
            approval
            for approval in (self._with_decision(r) for r in rows)
            if not approval.decided and not approval.is_expired(now)
        ]

    def close(self) -> None:
        """Close the handle."""
        self._db.close()

    def __enter__(self) -> DurableApprovals:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ------------------------------------------------------------ internals

    def _with_decision(self, row: sqlite3.Row) -> PendingApproval:
        decision = self._db.execute(
            "SELECT * FROM approval_decisions WHERE request_id = ?",
            (str(row["id"]),),
        ).fetchone()
        return PendingApproval(row, decision)

    def _by_fingerprint(self, fingerprint: str) -> PendingApproval | None:
        row = self._db.execute(
            "SELECT * FROM approval_requests WHERE fingerprint = ? "
            "ORDER BY requested_at DESC, id DESC LIMIT 1",
            (fingerprint,),
        ).fetchone()
        return self._with_decision(row) if row else None

    def _last_recorded(self) -> float:
        """The latest timestamp already in the file.

        A counter that restarts with the process is exactly wrong for a
        store built to outlive it: the second worker would stamp its
        request earlier than the first worker's, and "the newest request"
        would stop meaning anything. Seeding from the file makes the clock
        as durable as the rows it stamps.
        """
        row = self._db.execute(
            "SELECT MAX(t) AS t FROM ("
            "  SELECT MAX(requested_at) AS t FROM approval_requests"
            "  UNION ALL SELECT MAX(decided_at) FROM approval_decisions)"
        ).fetchone()
        return float(row["t"] or 1000.0)


def _ticks(start: float = 1000.0) -> Any:
    """A deterministic clock that advances one second per call."""
    counter = {"n": float(start)}

    def clock() -> float:
        counter["n"] += 1.0
        return counter["n"]

    return clock
