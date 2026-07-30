"""Per-session local storage, and the checkpointer adapter over it.

Each session's state lives *with* its compute rather than in a shared
database behind a network hop. Reads are local and fast, and there is no
per-session contention with other sessions. That locality is the edge
model's real benefit; the latency win is on state access, not on
inference, because unless you are using the platform's own models the model
call still leaves the edge.

:class:`LocalStore` is the platform's per-object SQL storage, reduced to
what the agent actually needs: a durable key-value map with an identity.
:class:`StorageCheckpointer` is the twelve-line adapter that makes it a
``Checkpointer``, and it is the entire storage-specific surface. Keeping it
this small is what makes the vendor concentration at the edge survivable.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from northstar_contracts import RunState

__all__ = ["LocalStore", "StorageCheckpointer"]


class LocalStore:
    """One session's durable storage. SQL, local to this object.

    Args:
        session_id: The object's durable identity. Two sessions never
            share a store, which is the isolation boundary Chapter 12 asks
            for, obtained structurally rather than configured.
        path: File to persist to. ``":memory:"`` keeps it in the process,
            which the tests use; a real edge object gets a file that
            outlives every hibernation.

    Example:
        >>> store = LocalStore("sess-1")
        >>> store.put("state", {"step": 3})
        >>> store.get("state")["step"]
        3
    """

    _SCHEMA = """
    CREATE TABLE IF NOT EXISTS kv (
        key   TEXT PRIMARY KEY,
        value TEXT NOT NULL
    );
    """

    def __init__(self, session_id: str, path: str | Path = ":memory:") -> None:
        self.session_id = session_id
        self.path = str(path)
        self.writes = 0
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.executescript(self._SCHEMA)
        self._conn.commit()

    def put(self, key: str, value: Any) -> None:
        """Store one value. Durable the moment it returns."""
        payload = json.dumps(value, sort_keys=True)
        with self._conn:
            self._conn.execute(
                "INSERT INTO kv (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, payload),
            )
        self.writes += 1

    def get(self, key: str) -> Any:
        """Read one value, or ``None``."""
        row = self._conn.execute(
            "SELECT value FROM kv WHERE key = ?", (key,)
        ).fetchone()
        return json.loads(row[0]) if row else None

    def keys(self) -> list[str]:
        """Every key, sorted."""
        return [
            r[0]
            for r in self._conn.execute(
                "SELECT key FROM kv ORDER BY key"
            ).fetchall()
        ]

    def close(self) -> None:
        """Release the database."""
        self._conn.close()


class StorageCheckpointer:
    """A ``Checkpointer`` over one session's local store.

    Twelve lines, and it is the entire storage-specific surface of the edge
    deployment. Everything above it — the loop, the tools, the policy, the
    graders — is the same code the Kubernetes worker runs.
    """

    def __init__(self, storage: LocalStore) -> None:
        self.storage = storage

    def save(self, state: RunState) -> None:
        """Persist the run. Survives hibernation, because it is on disk."""
        self.storage.put(f"run:{state.run_id}", state.to_dict())

    def load(self, run_id: str) -> RunState | None:
        """Restore the run, or ``None`` if this object has never seen it."""
        payload = self.storage.get(f"run:{run_id}")
        return RunState.from_dict(payload) if payload else None
