"""Cassettes are data exports with a shelf life.

Two rules keep them from rotting into liabilities.

**Redact on write, not on read.** A cassette captures the full request,
which for an agent includes the accumulated message history: customer
names, order contents, and whatever a tool returned three steps ago. A
recorded cassette is a data export, and it belongs in the repository only
after the redaction policy has run over it. Redacting on read leaves the
unredacted bytes on disk and in every clone.

**Give cassettes an expiry.** A replay suite that has passed unchanged for
eight months is not evidence that the agent still works. It is evidence
that the agent still works against a model that no longer exists.

The redaction itself is :class:`northstar_telemetry.Redactor`, which is the
same policy the traces use. Two redaction policies in one system means the
weaker one decides what leaks.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any

from northstar_telemetry import (
    DEFAULT_PATTERNS,
    DEFAULT_SENSITIVE_FIELDS,
    Redactor,
)

__all__ = [
    "MAX_AGE_DAYS",
    "Cassette",
    "expired",
    "load",
    "redactor",
    "unredacted",
]

#: A policy choice rather than a finding. Ninety days is long enough that
#: a stable suite is not constantly re-recording, and short enough that a
#: provider update cannot hide behind it for two quarters.
MAX_AGE_DAYS = 90

#: Fields a recorded exchange must never carry into the repository.
SENSITIVE_KEYS: frozenset[str] = frozenset(
    {"api_key", "authorization", "token", "customer_email", "request"}
)


def redactor() -> Redactor:
    """The policy that runs before bytes reach a cassette file.

    The shared field list and patterns, plus the fields a *recorded*
    exchange adds. Two redaction policies in one system means the weaker
    one decides what leaks, so this extends the trace policy rather than
    replacing it.
    """
    return Redactor(
        DEFAULT_SENSITIVE_FIELDS | SENSITIVE_KEYS, DEFAULT_PATTERNS
    )


@dataclass(frozen=True)
class Cassette:
    """One recorded file and its provenance.

    Args:
        path: The file.
        model: The model identifier the exchange was recorded against.
        provider: Which provider produced it.
        recorded_at: ``YYYY-MM-DD``. Empty means unstamped, which counts
            as expired: a cassette with no date cannot be aged out, so it
            never will be.
        records: How many exchanges it holds.
    """

    path: Path
    model: str
    provider: str
    recorded_at: str
    records: int

    def age_days(self, today: date | None = None) -> int | None:
        """Days since recording, or ``None`` when it is unstamped."""
        if not self.recorded_at:
            return None
        try:
            recorded = datetime.strptime(self.recorded_at, "%Y-%m-%d").date()
        except ValueError:
            return None
        return ((today or date.today()) - recorded).days

    def is_expired(
        self,
        today: date | None = None,
        max_age_days: int = MAX_AGE_DAYS,
    ) -> bool:
        """Whether this cassette is past its shelf life.

        An unstamped or unparseable date is expired, deliberately. Failing
        closed on missing provenance is the only way the check has teeth.
        """
        age = self.age_days(today)
        return age is None or age > max_age_days

    def to_dict(self) -> dict[str, Any]:
        """JSON-serialisable form, for a CI report."""
        return {
            "path": self.path.name,
            "model": self.model,
            "provider": self.provider,
            "recorded_at": self.recorded_at,
            "records": self.records,
        }


def load(path: str | Path) -> Cassette:
    """Read a cassette's provenance without loading it as a script.

    Raises:
        ValueError: If the file holds no response records.
    """
    file = Path(path)
    records = [
        json.loads(line)
        for line in file.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    responses = [r for r in records if r.get("kind") == "response"]
    if not responses:
        raise ValueError(f"{file.name} holds no recorded responses")
    first = responses[0]
    return Cassette(
        path=file,
        model=str(first.get("model", "")),
        provider=str(first.get("provider", "")),
        recorded_at=str(first.get("recorded_at", "")),
        records=len(responses),
    )


def unredacted(path: str | Path) -> list[str]:
    """Keys a cassette carries that the redaction policy forbids.

    Run this in CI over every cassette in the repository. A cassette that
    was recorded before the policy existed will fail here rather than
    quietly sitting in the history of every clone.
    """
    found: set[str] = set()
    text = Path(path).read_text(encoding="utf-8")
    for line in text.splitlines():
        if not line.strip():
            continue
        _walk(json.loads(line), found)
    return sorted(found)


def _walk(value: Any, found: set[str]) -> None:
    """Collect sensitive keys whose value was not masked."""
    if isinstance(value, dict):
        for key, item in value.items():
            if key.lower() in SENSITIVE_KEYS and not _masked(item):
                found.add(key)
            _walk(item, found)
    elif isinstance(value, list):
        for item in value:
            _walk(item, found)


def _masked(value: Any) -> bool:
    """Whether a value has been through the redactor."""
    return isinstance(value, str) and "REDACTED" in value.upper()


def expired(
    directory: str | Path,
    today: date | None = None,
    max_age_days: int = MAX_AGE_DAYS,
) -> list[Cassette]:
    """Every cassette in ``directory`` past its shelf life."""
    return [
        cassette
        for cassette in (
            load(path) for path in sorted(Path(directory).glob("*.jsonl"))
        )
        if cassette.is_expired(today, max_age_days)
    ]
