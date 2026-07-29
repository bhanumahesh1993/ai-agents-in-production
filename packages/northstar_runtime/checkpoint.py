"""Checkpointers: making a run survive the process that started it.

A checkpoint is the answer to one question: *if this worker dies right now,
what does the next worker need in order to carry on?* If the answer is
"nothing, we start again", you do not have an agent — you have a script
that sometimes charges a customer twice on restart.

Two implementations, deliberately:

* :class:`MemoryCheckpointer` — a dict. Fast, and honest about surviving
  nothing. Use it in tests.
* :class:`SqliteCheckpointer` — a single file, stdlib only, durable across
  a process restart. Use it locally, and swap in Postgres, Redis, or your
  cloud's session service in production. The interface is three methods
  wide precisely so that swap is cheap.
"""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from northstar_contracts import RunState

__all__ = [
    "Checkpointer",
    "MemoryCheckpointer",
    "SqliteCheckpointer",
]


@runtime_checkable
class Checkpointer(Protocol):
    """Persist and restore run state."""

    def save(self, state: RunState) -> None:
        """Write the current state. Must be safe to call repeatedly."""
        ...

    def load(self, run_id: str) -> RunState | None:
        """Return the latest state for a run, or ``None`` if unknown."""
        ...


class MemoryCheckpointer:
    """An in-process checkpointer.

    Survives an exception. Does not survive a restart, a deploy, or a pod
    eviction — which is the point of having it next to the SQLite one.

    Args:
        keep_history: Retain every version, not just the latest, so a test
            can assert on how a run progressed.
    """

    def __init__(self, keep_history: bool = True) -> None:
        self._latest: dict[str, dict[str, Any]] = {}
        self._history: dict[str, list[dict[str, Any]]] = {}
        self.keep_history = keep_history
        self.writes = 0

    def save(self, state: RunState) -> None:
        """Store a serialised copy of ``state``.

        The state is serialised rather than referenced, so a later
        in-place mutation of the live object cannot rewrite history.
        """
        payload = state.to_dict()
        self._latest[state.run_id] = payload
        if self.keep_history:
            self._history.setdefault(state.run_id, []).append(payload)
        self.writes += 1

    def load(self, run_id: str) -> RunState | None:
        """Return the latest state for ``run_id``."""
        payload = self._latest.get(run_id)
        return RunState.from_dict(payload) if payload else None

    def history(self, run_id: str) -> list[RunState]:
        """Every saved version of a run, oldest first."""
        return [RunState.from_dict(p) for p in self._history.get(run_id, [])]

    def run_ids(self) -> list[str]:
        """Every run this checkpointer knows about."""
        return sorted(self._latest)


class SqliteCheckpointer:
    """A checkpointer backed by one SQLite file.

    Args:
        path: Database file. ``":memory:"`` keeps it in the process, which
            is useful for tests but gives up durability.
        keep_history: Also append every version to a history table, so a
            run can be inspected step by step after the fact.

    Example:
        >>> cp = SqliteCheckpointer(":memory:")
        >>> cp.save(RunState(run_id="run-1", step=2))
        >>> loaded = cp.load("run-1")
        >>> loaded.step if loaded else None
        2
    """

    _SCHEMA = """
    CREATE TABLE IF NOT EXISTS checkpoints (
        run_id     TEXT PRIMARY KEY,
        step       INTEGER NOT NULL,
        status     TEXT NOT NULL,
        updated_at REAL NOT NULL,
        state      TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS checkpoint_history (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        run_id     TEXT NOT NULL,
        step       INTEGER NOT NULL,
        status     TEXT NOT NULL,
        written_at REAL NOT NULL,
        state      TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_history_run
        ON checkpoint_history (run_id, id);
    """

    def __init__(
        self,
        path: str | Path = ":memory:",
        *,
        keep_history: bool = True,
    ) -> None:
        self.path = str(path)
        self.keep_history = keep_history
        self.writes = 0
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(self._SCHEMA)
        self._conn.commit()

    def save(self, state: RunState) -> None:
        """Upsert the run's latest state, and append to history."""
        payload = json.dumps(state.to_dict(), sort_keys=True)
        now = time.time()
        with self._conn:
            self._conn.execute(
                "INSERT INTO checkpoints "
                "  (run_id, step, status, updated_at, state) "
                "VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(run_id) DO UPDATE SET "
                "  step=excluded.step, status=excluded.status, "
                "  updated_at=excluded.updated_at, state=excluded.state",
                (state.run_id, state.step, state.status, now, payload),
            )
            if self.keep_history:
                self._conn.execute(
                    "INSERT INTO checkpoint_history "
                    "  (run_id, step, status, written_at, state) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (state.run_id, state.step, state.status, now, payload),
                )
        self.writes += 1

    def load(self, run_id: str) -> RunState | None:
        """Return the latest state for ``run_id``."""
        row = self._conn.execute(
            "SELECT state FROM checkpoints WHERE run_id = ?", (run_id,)
        ).fetchone()
        if row is None:
            return None
        return RunState.from_dict(json.loads(row["state"]))

    def history(self, run_id: str) -> list[RunState]:
        """Every saved version of a run, oldest first."""
        rows = self._conn.execute(
            "SELECT state FROM checkpoint_history "
            "WHERE run_id = ? ORDER BY id",
            (run_id,),
        ).fetchall()
        return [RunState.from_dict(json.loads(r["state"])) for r in rows]

    def run_ids(self, status: str | None = None) -> list[str]:
        """Run ids, optionally filtered by status.

        ``run_ids(status="waiting_approval")`` is the query an approval
        inbox runs, and ``run_ids(status="running")`` is the one a
        supervisor runs after a restart to find work that was orphaned
        mid-flight.
        """
        if status is None:
            rows = self._conn.execute(
                "SELECT run_id FROM checkpoints ORDER BY run_id"
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT run_id FROM checkpoints WHERE status = ? "
                "ORDER BY run_id",
                (status,),
            ).fetchall()
        return [r["run_id"] for r in rows]

    def close(self) -> None:
        """Close the database connection."""
        self._conn.close()

    def __enter__(self) -> SqliteCheckpointer:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
