"""The append-only event log.

An agent run is only as debuggable as its event log. Every record is a
plain dict with the same five keys, so a log line means the same thing in a
unit test, in a JSONL file, and in a span exporter:

``run_id``, ``step``, ``type``, ``ts``, ``payload``.

The log is append-only. Nothing rewrites history: a correction is a new
record, not an edit. That is what makes replay and forensics possible.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Iterable, Iterator
from typing import Any

__all__ = [
    "EVENT_TYPES",
    "EventLog",
    "event_record",
]

#: The closed set of event types the runtime emits. A new type is an API
#: change: dashboards, graders, and the telemetry mapping all key off it.
EVENT_TYPES: frozenset[str] = frozenset(
    {
        "run.started",
        "model.called",
        "tool.called",
        "tool.result",
        "approval.requested",
        "approval.decided",
        "checkpoint.written",
        "run.finished",
    }
)


def event_record(
    run_id: str,
    step: int,
    type: str,
    payload: dict[str, Any] | None = None,
    *,
    ts: float | None = None,
) -> dict[str, Any]:
    """Build one event-log record.

    Args:
        run_id: The run the event belongs to.
        step: The loop step the event happened in.
        type: One of :data:`EVENT_TYPES`.
        payload: Event-specific fields. Copied shallowly, never mutated.
        ts: Unix timestamp. Defaults to now. Pass it explicitly when you
            need a deterministic log, for example in a golden-trajectory
            test.

    Returns:
        A JSON-serialisable dict.

    Raises:
        ValueError: If ``type`` is not a known event type.
    """
    if type not in EVENT_TYPES:
        known = ", ".join(sorted(EVENT_TYPES))
        raise ValueError(f"unknown event type {type!r}; expected one of {known}")
    return {
        "run_id": run_id,
        "step": step,
        "type": type,
        "ts": time.time() if ts is None else ts,
        "payload": dict(payload or {}),
    }


class EventLog:
    """An in-memory, append-only sequence of event records.

    Args:
        sink: Optional callback invoked with each record as it is appended.
            The telemetry layer uses this to turn events into spans without
            the loop needing to know a tracer exists.
    """

    def __init__(
        self,
        sink: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        self._records: list[dict[str, Any]] = []
        self._sink = sink

    def append(self, record: dict[str, Any]) -> dict[str, Any]:
        """Append a record and forward it to the sink. Returns the record."""
        self._records.append(record)
        if self._sink is not None:
            self._sink(record)
        return record

    def emit(
        self,
        run_id: str,
        step: int,
        type: str,
        payload: dict[str, Any] | None = None,
        *,
        ts: float | None = None,
    ) -> dict[str, Any]:
        """Build a record with :func:`event_record` and append it."""
        return self.append(event_record(run_id, step, type, payload, ts=ts))

    @property
    def records(self) -> list[dict[str, Any]]:
        """A copy of every record, oldest first."""
        return list(self._records)

    def of_type(self, *types: str) -> list[dict[str, Any]]:
        """Every record whose ``type`` is one of ``types``."""
        wanted = set(types)
        return [r for r in self._records if r["type"] in wanted]

    def for_run(self, run_id: str) -> list[dict[str, Any]]:
        """Every record belonging to one run."""
        return [r for r in self._records if r["run_id"] == run_id]

    def to_jsonl(self) -> str:
        """Render the log as JSON Lines, one record per line."""
        return "\n".join(json.dumps(r, sort_keys=True) for r in self._records)

    @classmethod
    def from_jsonl(cls, text: str) -> EventLog:
        """Parse a JSON Lines log written by :meth:`to_jsonl`."""
        log = cls()
        for line in text.splitlines():
            if line.strip():
                log.append(json.loads(line))
        return log

    def extend(self, records: Iterable[dict[str, Any]]) -> None:
        """Append many records in order."""
        for record in records:
            self.append(record)

    def __len__(self) -> int:
        return len(self._records)

    def __iter__(self) -> Iterator[dict[str, Any]]:
        return iter(list(self._records))

    def __repr__(self) -> str:
        return f"EventLog(records={len(self._records)})"
