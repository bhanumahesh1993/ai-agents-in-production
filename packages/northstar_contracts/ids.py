"""Canonical JSON, hashes, and idempotency keys.

Two agent behaviours in this book depend on turning a value into a stable
string: an idempotency key that survives a retry on another worker, and an
approval fingerprint that binds a human decision to one exact tool call.
Both need canonical JSON. ``json.dumps`` with default settings is not
canonical: key order and whitespace vary, so the same logical call hashes
two different ways and your exactly-once guarantee quietly evaporates.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

__all__ = [
    "canonical_json",
    "content_hash",
    "idempotency_key",
    "short_hash",
]


def canonical_json(value: Any) -> str:
    """Serialise ``value`` to a byte-stable JSON string.

    Keys are sorted, separators are tight, and non-ASCII is escaped, so two
    structurally equal values always produce the same string on any machine
    and any Python version.

    Args:
        value: Any JSON-serialisable value.

    Returns:
        The canonical JSON encoding.

    Raises:
        TypeError: If ``value`` contains something JSON cannot encode. That
            is intentional. A tool argument you cannot serialise is a tool
            argument you cannot fingerprint, journal, or replay.
    """
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )


def content_hash(value: Any) -> str:
    """Return the full sha256 hex digest of ``value``'s canonical JSON."""
    payload = canonical_json(value).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def short_hash(value: Any, length: int = 12) -> str:
    """Return a truncated :func:`content_hash`, for logs and span names."""
    return content_hash(value)[:length]


def idempotency_key(run_id: str, step_id: str | int) -> str:
    """Derive the idempotency key for one step of one run.

    The key is a pure function of ``(run_id, step_id)``. That is the whole
    trick: a retry after a timeout, a replay after a crash, and a second
    worker picking up the same journal all compute the same key, so the
    downstream service can recognise the second attempt as the same intent
    and refuse to move money twice.

    Deriving the key from wall-clock time or ``uuid4`` breaks this. It looks
    like it works, because the happy path never retries.

    Args:
        run_id: The run this step belongs to.
        step_id: The step index, or any stable per-step identifier.

    Returns:
        A 32-character hex string.
    """
    digest = hashlib.sha256(f"{run_id}:{step_id}".encode()).hexdigest()
    return digest[:32]
