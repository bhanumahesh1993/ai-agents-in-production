"""The refund service, with its idempotency table in a file.

An idempotency key is worth exactly nothing unless the *target* system
honours it. ``World`` honours one, which is enough for Chapter 1, where both
attempts happen inside one process. This chapter kills the process, so the
receipt has to outlive it: a second worker on a second machine presents the
same key and must be told "you already did this" by something that was still
around when it asked.

So this is the refund service, standing where a payments API would stand. It
holds a key-to-receipt table in the same SQLite file as the checkpoint, and
it writes that receipt *before* it answers, which is why a caller that never
hears the answer is still safe. That ordering is the whole reason
at-least-once delivery plus a derived key adds up to effectively-once
behaviour.

The service is also the reason the resumed run in ``demo.py`` can be a real
second process rather than a second function call.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

from northstar_contracts import Money, ToolSpec, ToolTimeout, World

__all__ = ["RefundLedger"]


class RefundLedger:
    """A refund tool whose idempotency receipts survive the process.

    Args:
        world: The in-memory store the refund actually lands in.
        path: SQLite file holding the receipts. Use the same file as the
            checkpointer: one file to delete between runs, and one file to
            open when you want to know what really happened.

    Example:
        >>> world = World()
        >>> service = RefundLedger(world, ":memory:")
        >>> first = service.issue_refund("NR-2026-0041827", 3250, "damaged",
        ...                              idempotency_key="k1")
        >>> again = service.issue_refund("NR-2026-0041827", 3250, "damaged",
        ...                              idempotency_key="k1")
        >>> again["duplicate"], service.total_cents("NR-2026-0041827")
        (True, 3250)
    """

    # One row per refund actually paid, keyed or not. The UNIQUE index is
    # what makes a key mean something: a second insert under the same key
    # is ignored. SQLite allows many NULLs in a UNIQUE column, which is
    # exactly right — an unkeyed refund is not the same intent as any other
    # unkeyed refund, and the service has no way to claim otherwise.
    _SCHEMA = """
    CREATE TABLE IF NOT EXISTS refund_receipts (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        idempotency_key TEXT UNIQUE,
        refund_id       TEXT NOT NULL,
        order_id        TEXT NOT NULL,
        amount_cents    INTEGER NOT NULL,
        reason          TEXT NOT NULL
    );
    """

    def __init__(self, world: World, path: str | Path = ":memory:") -> None:
        self.world = world
        self.path = str(path)
        self._db = sqlite3.connect(self.path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.executescript(self._SCHEMA)
        self._db.commit()

    # -- the tool ---------------------------------------------------------

    def issue_refund(
        self,
        order_id: str,
        amount_cents: Money,
        reason: str,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """Refund an order once per key, whatever the caller does next.

        Args:
            order_id: Order to refund against.
            amount_cents: Integer cents. Never a float.
            reason: One of the reasons ``get_policy`` knows.
            idempotency_key: Derived from ``(run_id, step_id)``. Omit it and
                a second attempt pays a second time, which is correct
                behaviour for a service that cannot tell two identical
                intents apart.

        Returns:
            The receipt, with ``duplicate`` set when the key was already
            spent.

        Raises:
            ToolTimeout: When the world's injected fault fires. The receipt
                is durable before the exception leaves this method.
        """
        if idempotency_key:
            prior = self.receipt(idempotency_key)
            if prior is not None:
                # The second attempt observes the first attempt's outcome.
                # No new money moves, and no state in the calling process
                # was needed to establish that.
                return {**prior, "duplicate": True}

        try:
            record = self.world.issue_refund(
                order_id, amount_cents, reason, idempotency_key
            )
        except ToolTimeout:
            # The write landed and the response was lost. A real service
            # commits the receipt before it answers, so the key is spent
            # even though this caller will never learn that.
            self._commit_landed(order_id, idempotency_key)
            raise

        self._commit(idempotency_key, record)
        return record

    def binding(self) -> tuple[ToolSpec, Any]:
        """The ``(spec, fn)`` pair to register instead of the world's own.

        The contract is the world's, unchanged: the agent cannot tell that
        the refund now goes through a service with a durable receipt table,
        and it should not be able to.
        """
        spec = next(
            s for s in self.world.tool_specs() if s.name == "issue_refund"
        )
        return spec, self.issue_refund

    def tools(self) -> list[tuple[ToolSpec, Any]]:
        """Every Northstar tool, with ``issue_refund`` replaced by this one."""
        return [
            self.binding() if spec.name == "issue_refund" else (spec, fn)
            for spec, fn in self.world.tools()
        ]

    # -- reading the receipts ---------------------------------------------

    def receipt(self, idempotency_key: str) -> dict[str, Any] | None:
        """The receipt for one key, or ``None`` if it has not been spent."""
        row = self._db.execute(
            "SELECT refund_id, order_id, amount_cents, reason "
            "FROM refund_receipts WHERE idempotency_key = ?",
            (idempotency_key,),
        ).fetchone()
        if row is None:
            return None
        return {
            "refund_id": str(row["refund_id"]),
            "order_id": str(row["order_id"]),
            "amount_cents": int(row["amount_cents"]),
            "reason": str(row["reason"]),
        }

    def rows(self, order_id: str | None = None) -> list[dict[str, Any]]:
        """Every refund the service has paid, oldest first.

        This is the ledger a grader reads and an auditor asks for. It is not
        the agent's account of itself, and the difference between the two is
        what Chapter 1 is about.
        """
        if order_id is None:
            rows = self._db.execute(
                "SELECT refund_id, order_id, amount_cents, reason "
                "FROM refund_receipts ORDER BY rowid"
            ).fetchall()
        else:
            rows = self._db.execute(
                "SELECT refund_id, order_id, amount_cents, reason "
                "FROM refund_receipts WHERE order_id = ? ORDER BY rowid",
                (order_id,),
            ).fetchall()
        return [
            {
                "refund_id": str(r["refund_id"]),
                "order_id": str(r["order_id"]),
                "amount_cents": int(r["amount_cents"]),
                "reason": str(r["reason"]),
            }
            for r in rows
        ]

    def total_cents(self, order_id: str) -> Money:
        """Sum of every refund the service has paid on one order."""
        return sum(r["amount_cents"] for r in self.rows(order_id))

    def hydrate(self) -> None:
        """Replay the stored receipts into this process's ``World``.

        A resumed worker starts with an empty in-memory view and a full
        durable record. Rebuilding the view from the record, rather than
        assuming it, is the difference between a resume and a fresh start
        that happens to know a run id.
        """
        for row in self.rows():
            key = self._key_of(row["refund_id"])
            if key and self.world.total_refunded_cents(row["order_id"]) == 0:
                self.world.issue_refund(
                    row["order_id"],
                    row["amount_cents"],
                    row["reason"],
                    idempotency_key=key,
                )

    def close(self) -> None:
        """Close the database connection."""
        self._db.close()

    # -- internals --------------------------------------------------------

    def _commit(
        self,
        idempotency_key: str | None,
        record: dict[str, Any],
    ) -> None:
        """Record one refund that was paid.

        An unkeyed call is recorded too — the money moved, so the ledger has
        to say so — but under a NULL key, which means nothing can ever
        recognise a second attempt at it.
        """
        self._db.execute(
            "INSERT OR IGNORE INTO refund_receipts "
            "(idempotency_key, refund_id, order_id, amount_cents, reason) "
            "VALUES (?,?,?,?,?)",
            (
                idempotency_key,
                record["refund_id"],
                record["order_id"],
                record["amount_cents"],
                record["reason"],
            ),
        )
        self._db.commit()

    def _commit_landed(
        self,
        order_id: str,
        idempotency_key: str | None,
    ) -> None:
        """Store the receipt for a write that landed before the timeout."""
        landed = [
            e
            for e in self.world.effects("refund_issued")
            if e["order_id"] == order_id
        ]
        if landed:
            self._commit(idempotency_key, landed[-1])

    def _key_of(self, refund_id: str) -> str | None:
        """The key a stored receipt was filed under, if it had one."""
        row = self._db.execute(
            "SELECT idempotency_key FROM refund_receipts WHERE refund_id = ?",
            (refund_id,),
        ).fetchone()
        if row is None or row["idempotency_key"] is None:
            return None
        return str(row["idempotency_key"])
