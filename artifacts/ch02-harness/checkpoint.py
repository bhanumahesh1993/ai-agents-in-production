"""Checkpointers, and the one line of SQL worth copying.

``Checkpointer`` is a two-method protocol, ``save(state)`` and
``load(run_id)``, and this chapter ships two implementations of it.
``MemoryCheckpointer`` is a dictionary, imported from
``northstar_runtime`` and re-exported here: it is honest about being a test
double, proving the loop calls the interface at the right boundaries and
proving nothing at all about durability. :class:`SqliteCheckpointer` is the
real one.

It differs from ``northstar_runtime.SqliteCheckpointer`` in the two ways
Chapter 2 argues for. It stores the effective configuration hash beside the
state, so a resume can refuse to continue a run under a different model or
system prompt. And its upsert is guarded by the step, so a worker that the
scheduler paused for forty seconds cannot wake up and write its stale view
over a checkpoint another worker has already advanced. That is a rare bug,
it looks exactly like data corruption, and the fix is one line of SQL.

Nine things belong in a checkpoint: the run id and step, the status, the
message history, the budget consumed, the configuration hash, any pending
call with its derived key, any completed result, approval records with
their fingerprints, and the trace identifiers. Six must stay out: sockets,
in-memory tool handles, credentials, absolute wall-clock deadlines, model
client objects, and raw personal data you would not want in a forensic
record.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from northstar_contracts import (
    Message,
    RunState,
    ToolResult,
    ToolSpec,
    canonical_json,
    content_hash,
)
from northstar_runtime import Checkpointer, MemoryCheckpointer

__all__ = [
    "MemoryCheckpointer",
    "SqliteCheckpointer",
    "config_hash_for",
    "decode",
    "encode",
]


def config_hash_for(
    model: str,
    system_prompt: str,
    specs: list[ToolSpec],
    decoding: dict[str, Any] | None = None,
) -> str:
    """Hash the configuration that produced a run's behaviour.

    Model, system instructions, tool versions, and decoding settings. This
    is the answer to "which configuration produced this run", which is a
    question you will be asked about a specific run three months later, and
    it is what :class:`~runner.HarnessRunner` compares before it agrees to
    resume anything.
    """
    return content_hash(
        {
            "model": model,
            "system_prompt": system_prompt,
            "tools": sorted((s.name, s.version) for s in specs),
            "decoding": dict(decoding or {}),
        }
    )[:16]


def encode(messages: list[Message]) -> str:
    """Serialise a message list to JSON, tool results included.

    The loop appends ``Message(role="tool", content=result)`` with a live
    ``ToolResult``, because that is the object the model's observation
    actually is. A checkpoint you cannot write to disk is not a checkpoint,
    so the dataclass is flattened here rather than at every call site.
    """
    payload = []
    for message in messages:
        content = message.content
        if isinstance(content, ToolResult):
            content = {"__tool_result__": content.to_dict()}
        payload.append({"role": message.role, "content": content})
    return canonical_json(payload)


def decode(blob: str) -> list[Message]:
    """Rebuild the message list :func:`encode` wrote."""
    messages: list[Message] = []
    for entry in json.loads(blob):
        content = entry["content"]
        if isinstance(content, dict) and "__tool_result__" in content:
            content = ToolResult.from_dict(content["__tool_result__"])
        messages.append(Message(role=entry["role"], content=content))
    return messages


class SqliteCheckpointer(Checkpointer):
    """Run state in one SQLite file, with a stale-write guard.

    Args:
        path: Database file. ``":memory:"`` is useful in tests and gives up
            the only property that matters in production.
        config_hash: The effective configuration this process is running.
            Stored with every checkpoint and compared on resume.

    Example:
        >>> cp = SqliteCheckpointer(":memory:", config_hash="abc")
        >>> cp.save(RunState(run_id="run-1", step=4))
        >>> cp.save(RunState(run_id="run-1", step=2))   # a stale worker
        >>> loaded = cp.load("run-1")
        >>> loaded.step if loaded else None
        4
    """

    _SCHEMA = """
    CREATE TABLE IF NOT EXISTS checkpoints (
        run_id             TEXT PRIMARY KEY,
        step               INTEGER NOT NULL,
        status             TEXT NOT NULL,
        budget_spent_cents INTEGER NOT NULL,
        config_hash        TEXT NOT NULL,
        blob               TEXT NOT NULL
    );
    """

    def __init__(
        self,
        path: str | Path = ":memory:",
        *,
        config_hash: str = "",
    ) -> None:
        self.path = str(path)
        self.config_hash = config_hash
        self.writes = 0
        self._db = sqlite3.connect(self.path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.executescript(self._SCHEMA)
        self._db.commit()

    def save(self, state: RunState) -> None:
        # Upsert keyed on run_id, guarded by step: saving the
        # same state twice is a no-op, and a slow worker cannot
        # overwrite a newer checkpoint with a stale one.
        self._db.execute(
            "INSERT INTO checkpoints(run_id, step, status, "
            "budget_spent_cents, config_hash, blob) "
            "VALUES (?,?,?,?,?,?) ON CONFLICT(run_id) DO UPDATE "
            "SET step=excluded.step, status=excluded.status, "
            "budget_spent_cents=excluded.budget_spent_cents, "
            "blob=excluded.blob "
            "WHERE excluded.step >= checkpoints.step",
            (state.run_id, state.step, state.status,
             state.budget_spent_cents, self.config_hash,
             encode(state.messages)),
        )
        self._db.commit()
        self.writes += 1

    def load(self, run_id: str) -> RunState | None:
        """Return the latest state for ``run_id``, or ``None``."""
        row = self._db.execute(
            "SELECT step, status, budget_spent_cents, blob "
            "FROM checkpoints WHERE run_id = ?",
            (run_id,),
        ).fetchone()
        if row is None:
            return None
        return RunState(
            run_id=run_id,
            step=int(row["step"]),
            messages=decode(row["blob"]),
            status=row["status"],
            budget_spent_cents=int(row["budget_spent_cents"]),
        )

    def stored_config_hash(self, run_id: str) -> str | None:
        """The configuration hash the run was started under."""
        row = self._db.execute(
            "SELECT config_hash FROM checkpoints WHERE run_id = ?",
            (run_id,),
        ).fetchone()
        return str(row["config_hash"]) if row else None

    def run_ids(self, status: str | None = None) -> list[str]:
        """Run ids, optionally filtered by status.

        ``run_ids(status="running")`` is the query a supervisor runs after a
        restart to find work that was orphaned mid-flight.
        """
        if status is None:
            rows = self._db.execute(
                "SELECT run_id FROM checkpoints ORDER BY run_id"
            ).fetchall()
        else:
            rows = self._db.execute(
                "SELECT run_id FROM checkpoints WHERE status = ? "
                "ORDER BY run_id",
                (status,),
            ).fetchall()
        return [str(r["run_id"]) for r in rows]

    def close(self) -> None:
        """Close the database connection."""
        self._db.close()
