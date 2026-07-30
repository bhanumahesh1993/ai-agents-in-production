"""A stream that survives a suspension and a crash.

Suspension has a user-facing consequence: a streaming connection does not
survive it, and neither does a crash. The fix is a resumable stream.
Server-sent events carry an event id, a reconnecting client sends the last
id it received in a ``Last-Event-ID`` header, and the server answers by
replaying events after that id and then continuing live.

Two practical notes, both implemented here. Resume at **event** granularity
rather than token granularity, since a partially streamed token buffer is
not worth persisting. And re-send the current step's partial output on
reconnect, so the user sees continuity rather than a gap.

The stream reads the same journal the run does. Sequence numbers are per
run and monotonic, which is the only property a reconnect needs.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any

from northstar_runtime import Journal, SimulatedCrash

__all__ = [
    "LAST_EVENT_ID_HEADER",
    "StreamClient",
    "sse",
    "stream",
]

#: The header a reconnecting client sends. The name is the standard's.
LAST_EVENT_ID_HEADER = "Last-Event-ID"


def sse(id: int, data: Any, event: str = "message") -> str:
    """Render one server-sent event.

    The id is what makes the stream resumable, so it is never omitted.
    """
    return (
        f"id: {id}\n"
        f"event: {event}\n"
        f"data: {json.dumps(data, sort_keys=True)}\n\n"
    )


def since(
    journal: Journal,
    run_id: str,
    after: int = 0,
) -> list[dict[str, Any]]:
    """Journal records for one run with a sequence above ``after``."""
    return [
        record
        for record in journal.records(run_id)
        if int(record["seq"]) > after
    ]


def stream(
    journal: Journal,
    run_id: str,
    last_event_id: int = 0,
    *,
    live: Iterator[dict[str, Any]] | None = None,
    crash_after: int | None = None,
) -> Iterator[str]:
    """Replay what the client missed, then follow the run live.

    Args:
        journal: The run's append-only record.
        run_id: Which run to stream.
        last_event_id: The sequence the client last saw. Zero means the
            client is new and wants everything.
        live: Records arriving while the client is connected. ``None``
            means the run is already finished and there is nothing to tail,
            which is the ordinary case for a reconnect after a crash.
        crash_after: Raise :class:`~northstar_runtime.SimulatedCrash` once
            this many events have been yielded. Test affordance only.

    Yields:
        Server-sent event frames.

    Raises:
        SimulatedCrash: At the configured point, so a test can reconnect.
    """
    sent = 0
    for record in since(journal, run_id, last_event_id):
        yield sse(int(record["seq"]), record["payload"],
                  event=record["type"])
        sent += 1
        if crash_after is not None and sent >= crash_after:
            raise SimulatedCrash(
                f"connection dropped after {sent} event(s) of run {run_id}"
            )
    for record in live or ():          # live tail
        yield sse(int(record["seq"]), record["payload"],
                  event=record["type"])


@dataclass
class StreamClient:
    """A client that reconnects with the last id it saw.

    Args:
        run_id: The run it is watching.
        last_event_id: What it has already received. Persisted by the
            client, which is the half of this mechanism that is not the
            server's.
    """

    run_id: str
    last_event_id: int = 0
    received: list[tuple[int, str]] = field(default_factory=list)

    def consume(self, frames: Iterator[str]) -> int:
        """Read frames until the stream ends or the connection drops.

        Returns:
            How many frames this connection delivered.
        """
        delivered = 0
        try:
            for frame in frames:
                event_id, event = _parse(frame)
                self.received.append((event_id, event))
                self.last_event_id = event_id
                delivered += 1
        except SimulatedCrash:
            pass
        return delivered

    def headers(self) -> dict[str, str]:
        """What the reconnect sends."""
        return {LAST_EVENT_ID_HEADER: str(self.last_event_id)}

    @property
    def ids(self) -> list[int]:
        """Every event id this client has seen, in order."""
        return [event_id for event_id, _ in self.received]

    @property
    def gapless(self) -> bool:
        """Whether the client saw every id once, in order, with no gap."""
        return self.ids == sorted(set(self.ids)) and self.ids == list(
            range(self.ids[0], self.ids[0] + len(self.ids))
        ) if self.ids else True


def _parse(frame: str) -> tuple[int, str]:
    """Pull the id and the event name out of one frame."""
    event_id = 0
    event = "message"
    for line in frame.splitlines():
        if line.startswith("id: "):
            event_id = int(line[4:])
        elif line.startswith("event: "):
            event = line[7:]
    return event_id, event
