"""The checkpoint envelope: a run state plus the version that produced it.

``RunState`` is the payload. The envelope is what a resume path needs
*around* it, and the field most often omitted is the one the opening
incident turned on.

A checkpoint carries the run identity and step position, the status, the
message history, the budget consumed, the deadline, the principal and
scopes, the pending tool call if there is one, and **the version of the
agent configuration that produced all of it**. "Version" here is not the
container tag: it is the effective configuration -- system prompt, tool
specs and their versions, policy bundle, model identifier, checkpoint
schema -- and that set has a hash. The hash belongs in the checkpoint, and
the resume path compares it before doing anything else.

What a checkpoint must never carry: open sockets, live model and tool
clients, values derived from a wall clock that will be wrong when they are
read, and data the run cannot legally hold for its retention window.
Something sitting in storage for three days falls under your retention and
residency policy in a way a stack variable does not.

The state itself goes through :class:`SqliteCheckpointer` from the runtime
package, unchanged. This module adds the sidecar table that carries the
envelope, in the same file, so one file holds the whole run.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from northstar_contracts import RunState, content_hash
from northstar_runtime import SqliteCheckpointer
from states import RunPhase, Transition, check_transition

__all__ = [
    "SCHEMA_VERSION",
    "ConfigDrift",
    "Envelope",
    "EnvelopeStore",
    "UnknownRun",
    "config_hash",
]

#: Bumped when the shape of what is stored changes. Separate from the
#: agent version on purpose: a checkpoint can be readable and still have
#: been produced by an agent whose behaviour has changed.
SCHEMA_VERSION = "1"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS envelopes (
    run_id         TEXT PRIMARY KEY,
    agent_version  TEXT NOT NULL,
    schema_version TEXT NOT NULL,
    config_hash    TEXT NOT NULL,
    phase          TEXT NOT NULL,
    pending_call   TEXT,
    deadline_at    REAL NOT NULL,
    principal      TEXT NOT NULL,
    updated_at     REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS transitions (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id   TEXT NOT NULL,
    source   TEXT NOT NULL,
    target   TEXT NOT NULL,
    at       REAL NOT NULL,
    actor    TEXT NOT NULL,
    reason   TEXT NOT NULL
);
"""


class UnknownRun(KeyError):
    """No envelope for that run id."""


class ConfigDrift(RuntimeError):
    """The checkpoint was written by a configuration this worker is not.

    Raised only when no migration path is declared. Never deserialise old
    state into new code without a version check: a run started under one
    configuration hash and finished under another has been silently
    migrated by nobody.
    """

    def __init__(self, run_id: str, stored: str, running: str) -> None:
        self.run_id = run_id
        self.stored = stored
        self.running = running
        super().__init__(
            f"run {run_id} was checkpointed by agent {stored!r} and this "
            f"worker is {running!r}; pin, migrate, or cancel -- do not guess"
        )


def config_hash(
    agent_version: str,
    system_prompt: str,
    tool_versions: dict[str, str],
    policy_bundle: str,
    model: str,
) -> str:
    """Hash the effective configuration, not the container tag.

    Everything the agent's behaviour depends on and nothing it does not.
    Two workers with the same hash will behave the same way on the same
    checkpoint, which is the only property the resume path needs.
    """
    return content_hash(
        {
            "agent_version": agent_version,
            "system_prompt": system_prompt,
            "tool_versions": dict(sorted(tool_versions.items())),
            "policy_bundle": policy_bundle,
            "model": model,
        }
    )[:16]


@dataclass
class Envelope:
    """A run state with the metadata a different process needs to use it."""

    state: RunState
    agent_version: str
    schema_version: str = SCHEMA_VERSION
    config_hash: str = ""
    phase: RunPhase = RunPhase.QUEUED
    pending_call: dict[str, Any] | None = None
    deadline_at: float = 0.0
    principal: dict[str, Any] = field(default_factory=dict)

    @property
    def run_id(self) -> str:
        """The run this envelope belongs to."""
        return self.state.run_id


class EnvelopeStore:
    """Checkpoints, versions, and the transition log, in one file.

    Args:
        path: SQLite file. The side-effect ledger uses the same one.
        clock: Injectable time source, so the demo's timestamps are
            deterministic and a test never sleeps.
    """

    def __init__(
        self,
        path: str | Path,
        clock: Any | None = None,
    ) -> None:
        self.path = str(path)
        self.checkpointer = SqliteCheckpointer(self.path)
        self._db = sqlite3.connect(self.path)
        self._db.row_factory = sqlite3.Row
        self._db.executescript(_SCHEMA)
        self._db.commit()
        self._clock = clock or _monotonic_ticks()

    # ------------------------------------------------------------- writing

    def save(self, envelope: Envelope) -> None:
        """Persist the state and its envelope. The state goes first.

        Ordering matters here for the same reason it matters around a side
        effect: a version row pointing at a state that was never written is
        worse than no row at all.
        """
        self.checkpointer.save(envelope.state)
        self._db.execute(
            "INSERT INTO envelopes (run_id, agent_version, schema_version, "
            " config_hash, phase, pending_call, deadline_at, principal, "
            " updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(run_id) DO UPDATE SET "
            " agent_version=excluded.agent_version, "
            " schema_version=excluded.schema_version, "
            " config_hash=excluded.config_hash, "
            " phase=excluded.phase, "
            " pending_call=excluded.pending_call, "
            " deadline_at=excluded.deadline_at, "
            " principal=excluded.principal, "
            " updated_at=excluded.updated_at",
            (
                envelope.run_id,
                envelope.agent_version,
                envelope.schema_version,
                envelope.config_hash,
                envelope.phase.value,
                json.dumps(envelope.pending_call)
                if envelope.pending_call
                else None,
                envelope.deadline_at,
                json.dumps(envelope.principal, sort_keys=True),
                self._clock(),
            ),
        )
        self._db.commit()

    def transition(
        self,
        envelope: Envelope,
        target: RunPhase,
        actor: str,
        reason: str,
    ) -> Transition:
        """Move the run and append the move to the log.

        Every transition is an append-only event carrying a timestamp, an
        actor, and a reason, which is what makes a run's history answerable
        three months later.

        Raises:
            IllegalTransition: If the move is not declared legal.
        """
        check_transition(envelope.phase, target)
        record = Transition(
            run_id=envelope.run_id,
            source=envelope.phase,
            target=target,
            at=self._clock(),
            actor=actor,
            reason=reason,
        )
        envelope.phase = target
        envelope.state = envelope.state.with_status(_run_status(target))
        self._db.execute(
            "INSERT INTO transitions (run_id, source, target, at, actor, "
            " reason) VALUES (?, ?, ?, ?, ?, ?)",
            (
                record.run_id,
                record.source.value,
                record.target.value,
                record.at,
                record.actor,
                record.reason,
            ),
        )
        self._db.commit()
        self.save(envelope)
        return record

    # ------------------------------------------------------------- reading

    def load_envelope(self, run_id: str) -> Envelope:
        """Load a run's state and its envelope.

        Raises:
            UnknownRun: If nothing was ever checkpointed under that id.
                An unknown run is not an empty run; guessing an initial
                state for one is how a resume silently starts over.
        """
        row = self._db.execute(
            "SELECT * FROM envelopes WHERE run_id = ?", (run_id,)
        ).fetchone()
        state = self.checkpointer.load(run_id)
        if row is None or state is None:
            raise UnknownRun(f"no checkpoint for run {run_id!r}")
        return Envelope(
            state=state,
            agent_version=str(row["agent_version"]),
            schema_version=str(row["schema_version"]),
            config_hash=str(row["config_hash"]),
            phase=RunPhase(str(row["phase"])),
            pending_call=(
                json.loads(str(row["pending_call"]))
                if row["pending_call"]
                else None
            ),
            deadline_at=float(row["deadline_at"]),
            principal=json.loads(str(row["principal"])),
        )

    def history(self, run_id: str) -> list[Transition]:
        """Every recorded transition for one run, oldest first."""
        rows = self._db.execute(
            "SELECT * FROM transitions WHERE run_id = ? ORDER BY id",
            (run_id,),
        ).fetchall()
        return [
            Transition(
                run_id=str(r["run_id"]),
                source=RunPhase(str(r["source"])),
                target=RunPhase(str(r["target"])),
                at=float(r["at"]),
                actor=str(r["actor"]),
                reason=str(r["reason"]),
            )
            for r in rows
        ]

    def parked(self) -> list[str]:
        """Runs in ``waiting_approval`` or ``suspended``.

        The pre-flight check a deploy needs: a pipeline that does not know
        these runs exist will happily expire the version they are pinned
        to.
        """
        rows = self._db.execute(
            "SELECT run_id FROM envelopes WHERE phase IN (?, ?)",
            (RunPhase.WAITING_APPROVAL.value, RunPhase.SUSPENDED.value),
        ).fetchall()
        return [str(r["run_id"]) for r in rows]

    def close(self) -> None:
        """Close both handles on the file."""
        self._db.close()
        self.checkpointer.close()

    def __enter__(self) -> EnvelopeStore:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def _run_status(phase: RunPhase) -> str:
    """Map a run phase onto the narrower ``RunStatus`` the contract holds.

    ``RunState`` knows five statuses because that is what the loop needs.
    The run record knows eight because that is what operations needs.
    Mapping in one place beats teaching either of them the other's
    vocabulary.
    """
    if phase in (RunPhase.QUEUED, RunPhase.RESUMING):
        return "running"
    if phase is RunPhase.SUSPENDED:
        return "waiting_approval"
    return phase.value


def _monotonic_ticks() -> Any:
    """A deterministic clock: one tick per call, starting at 1.

    Replay must be deterministic, so workflow code reads neither the wall
    clock nor a random source. Both come in as recorded inputs, and this is
    the recorded input.
    """
    counter = {"n": 0}

    def clock() -> float:
        counter["n"] += 1
        return float(counter["n"])

    return clock
