"""The intent journal, and the payment service that honours a key.

Two stores, deliberately in two files, because they belong to two parties.

:class:`SideEffectLedger` is yours. It records "about to call this tool
with these arguments under this key" *before* the call, and the outcome
after it. A worker that dies between the two writes comes back knowing a
call was attempted and not knowing whether it landed, which is the only
state from which you can do something sensible. The recovery rule for an
intent with no outcome is **resolve, do not repeat**.

:class:`RefundService` is not yours. It stands where a payments API
stands, in its own file, with its own connection, and it deliberately does
not know what an order is worth: a provider takes a charge reference, an
amount, and a key, dedupes on the key, and has no opinion about whether
your total makes sense. That is not a simplification for the example. It is
why the key is the only thing standing between a resumed run and a second
payout.

Separating the connections is what makes the broken-key demonstration
honest. ``record_intent(flush=False)`` leaves the journal write uncommitted,
the service commits its own transaction on its own connection, and a worker
that dies in between has moved money under a key no surviving record knows
about. On one shared connection the service's commit would have flushed
your journal too, and the failure would have been impossible to reproduce
for a reason that has nothing to do with the design.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from northstar_contracts import Money, canonical_json

__all__ = ["Intent", "RefundService", "SideEffectLedger"]

_LEDGER_SCHEMA = """
CREATE TABLE IF NOT EXISTS intents (
    idempotency_key TEXT PRIMARY KEY,
    run_id          TEXT NOT NULL,
    step_id         INTEGER NOT NULL,
    tool            TEXT NOT NULL,
    arguments       TEXT NOT NULL,
    recorded_at     REAL NOT NULL,
    outcome         TEXT
);
"""

_SERVICE_SCHEMA = """
CREATE TABLE IF NOT EXISTS settlements (
    settlement_id   INTEGER PRIMARY KEY AUTOINCREMENT,
    idempotency_key TEXT UNIQUE NOT NULL,
    kind            TEXT NOT NULL,
    order_id        TEXT NOT NULL,
    amount_cents    INTEGER NOT NULL,
    reason          TEXT NOT NULL
);
"""


class Intent:
    """One recorded intent, with or without its outcome."""

    def __init__(self, row: sqlite3.Row) -> None:
        self.key = str(row["idempotency_key"])
        self.run_id = str(row["run_id"])
        self.step_id = int(row["step_id"])
        self.tool = str(row["tool"])
        self.arguments: dict[str, Any] = json.loads(str(row["arguments"]))
        self.recorded_at = float(row["recorded_at"])
        self.outcome: dict[str, Any] | None = (
            json.loads(str(row["outcome"])) if row["outcome"] else None
        )

    @property
    def unresolved(self) -> bool:
        """An intent with no recorded outcome. The ambiguity window."""
        return self.outcome is None

    def render(self) -> str:
        """One line for the reconciliation report."""
        state = "UNRESOLVED" if self.unresolved else "settled"
        return (
            f"step {self.step_id:>2} {self.tool:<24} "
            f"key={self.key[:12]}... {state}"
        )


class SideEffectLedger:
    """Intent before the call, outcome after it, in a file you own.

    Args:
        path: SQLite file. Share it with the envelope store, so there is
            one file to delete between runs and one file to open when you
            want to know what your side of the conversation did.
    """

    def __init__(self, path: str | Path = ":memory:") -> None:
        self.path = str(path)
        self.connection = sqlite3.connect(self.path)
        self.connection.row_factory = sqlite3.Row
        self.connection.executescript(_LEDGER_SCHEMA)
        self.connection.commit()

    def record_intent(
        self,
        key: str,
        run_id: str,
        step_id: int,
        tool: str,
        arguments: dict[str, Any],
        at: float,
        *,
        flush: bool = True,
    ) -> None:
        """Write "about to do this", before the call.

        Args:
            flush: Commit the write. ``False`` models the implementation
                that generates a key and gets on with the call, trusting
                the store to catch up. It is the only difference between
                the two key strategies that is visible in this method, and
                it is enough to lose 24,000 cents.
        """
        self.connection.execute(
            "INSERT OR IGNORE INTO intents "
            "(idempotency_key, run_id, step_id, tool, arguments, "
            " recorded_at, outcome) VALUES (?, ?, ?, ?, ?, ?, NULL)",
            (key, run_id, step_id, tool, canonical_json(arguments), at),
        )
        if flush:
            self.connection.commit()

    def record_outcome(self, key: str, outcome: dict[str, Any]) -> None:
        """Write what happened, after the call returned."""
        self.connection.execute(
            "UPDATE intents SET outcome = ? WHERE idempotency_key = ?",
            (canonical_json(outcome), key),
        )
        self.connection.commit()

    def intent(self, key: str) -> Intent | None:
        """One intent by key."""
        row = self.connection.execute(
            "SELECT * FROM intents WHERE idempotency_key = ?", (key,)
        ).fetchone()
        return Intent(row) if row else None

    def intents(self, run_id: str | None = None) -> list[Intent]:
        """Every intent, oldest first, optionally for one run."""
        rows = self.connection.execute(
            "SELECT * FROM intents ORDER BY recorded_at, step_id"
        ).fetchall()
        return [
            Intent(r)
            for r in rows
            if run_id is None or str(r["run_id"]) == run_id
        ]

    def unresolved(self, run_id: str) -> list[Intent]:
        """Intents with no outcome. What a resumed worker must settle."""
        return [i for i in self.intents(run_id) if i.unresolved]

    def abandon(self) -> None:
        """Die without flushing. What a killed worker actually does."""
        self.connection.rollback()
        self.connection.close()

    def close(self) -> None:
        """Commit nothing extra and close."""
        self.connection.close()

    def __enter__(self) -> SideEffectLedger:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


class RefundService:
    """A payment provider stand-in that dedupes on the key you present.

    Args:
        path: Its own SQLite file. Not yours, and not in the same
            transaction as anything of yours.
    """

    def __init__(self, path: str | Path = ":memory:") -> None:
        self.path = str(path)
        self.connection = sqlite3.connect(self.path)
        self.connection.row_factory = sqlite3.Row
        self.connection.executescript(_SERVICE_SCHEMA)
        self.connection.commit()

    def settle(
        self,
        kind: str,
        order_id: str,
        amount_cents: Money,
        reason: str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        """Settle one effect, or return the original if the key is known.

        The row is written before the reply, which is why a caller that
        never hears the reply is still safe -- provided it can present the
        same key again.
        """
        prior = self.lookup(idempotency_key)
        if prior is not None:
            return {**prior, "duplicate": True}
        cursor = self.connection.execute(
            "INSERT INTO settlements "
            "(idempotency_key, kind, order_id, amount_cents, reason) "
            "VALUES (?, ?, ?, ?, ?)",
            (idempotency_key, kind, order_id, amount_cents, reason),
        )
        self.connection.commit()
        return {
            "settlement_id": f"STL-{int(cursor.lastrowid or 0):05d}",
            "kind": kind,
            "order_id": order_id,
            "amount_cents": amount_cents,
            "reason": reason,
            "idempotency_key": idempotency_key,
            "duplicate": False,
        }

    def lookup(self, idempotency_key: str) -> dict[str, Any] | None:
        """The independent read that resolves an intent without repeating.

        This is what "resolve, do not repeat" costs: one read against the
        authoritative system, by key. It is only available to a caller that
        can still compute the key.
        """
        row = self.connection.execute(
            "SELECT * FROM settlements WHERE idempotency_key = ?",
            (idempotency_key,),
        ).fetchone()
        return _record(row) if row else None

    def settlements(
        self,
        order_id: str | None = None,
        kind: str | None = None,
    ) -> list[dict[str, Any]]:
        """Every settlement, oldest first. The auditor's view."""
        rows = self.connection.execute(
            "SELECT * FROM settlements ORDER BY settlement_id"
        ).fetchall()
        return [
            _record(r)
            for r in rows
            if (order_id is None or str(r["order_id"]) == order_id)
            and (kind is None or str(r["kind"]) == kind)
        ]

    def total_cents(self, order_id: str, kind: str = "refund") -> Money:
        """What actually left the building against one order."""
        return sum(
            int(r["amount_cents"])
            for r in self.settlements(order_id=order_id, kind=kind)
        )

    def close(self) -> None:
        """Close the handle."""
        self.connection.close()

    def __enter__(self) -> RefundService:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def _record(row: sqlite3.Row) -> dict[str, Any]:
    """Render one settlement row."""
    return {
        "settlement_id": f"STL-{int(row['settlement_id']):05d}",
        "kind": str(row["kind"]),
        "order_id": str(row["order_id"]),
        "amount_cents": int(row["amount_cents"]),
        "reason": str(row["reason"]),
        "idempotency_key": str(row["idempotency_key"]),
    }
