"""Payload policy: what is exported, what is hashed, and what never leaves.

The most valuable content in an agent trace is also the content you are least
entitled to store, and the default in most SDKs is against you: payload
capture is on unless you configure it off. That default is right for local
development and wrong for a production system handling customer data.

A workable policy sorts every field into three buckets, and the sorting is a
design decision that belongs in code review rather than in a wiki:

**Never exported.** Raw customer message text, full tool-result bodies,
credentials in any form. Dropped at the instrumentation boundary *inside* the
process, before the exporter sees them. Dropping in the collector is not
equivalent, because the data has already crossed a network hop and a collector
misconfiguration then becomes a disclosure.

**Exported as a digest.** Identifiers you need to correlate but not to read.
A stable digest supports every grouping and comparison you actually perform on
these fields and none of the reading you should not be doing.

**Exported verbatim.** The small set of fields whose values *are* the evidence:
amounts in cents, currency, tool name and version, decision and status,
idempotency key, external transaction id, budget figures, and the policy
decision. These are business facts, not personal data, and they are what makes
reconciliation possible.

``Redactor`` here extends the one in ``northstar_telemetry`` with that
three-bucket constructor, so the policy is one reviewable object rather than a
set of scattered checks.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from northstar_contracts import short_hash
from northstar_telemetry import DEFAULT_PATTERNS
from northstar_telemetry import Redactor as _BaseRedactor

__all__ = ["REDACTOR", "Redactor"]


class Redactor(_BaseRedactor):
    """A field-level policy with three explicit buckets.

    Args:
        drop: Dotted field paths removed entirely. Matched on the last
            segment or on the full path, so ``"arguments.email"`` and
            ``"email"`` both work.
        hash: Paths replaced by a stable digest.
        keep: Paths exported verbatim. Listed rather than implied, because
            "everything else is fine" is how a new field ships unreviewed.
        patterns: Regexes applied to whatever string values survive, for the
            things that end up in a free-text field despite everyone's best
            intentions.

    Raises:
        ValueError: If a field appears in more than one bucket. A policy
            that says two things about one field is a policy nobody can
            review.
    """

    def __init__(
        self,
        drop: Iterable[str] = (),
        hash: Iterable[str] = (),          # noqa: A002 - the chapter's name
        keep: Iterable[str] = (),
        patterns: tuple[tuple[str, str], ...] = DEFAULT_PATTERNS,
    ) -> None:
        self.drop = [f.lower() for f in drop]
        self.hash = [f.lower() for f in hash]
        self.keep = [f.lower() for f in keep]

        overlap = (
            (set(self.drop) & set(self.hash))
            | (set(self.drop) & set(self.keep))
            | (set(self.hash) & set(self.keep))
        )
        if overlap:
            raise ValueError(
                "these fields appear in more than one bucket: "
                + ", ".join(sorted(overlap))
            )
        super().__init__(fields=(), patterns=patterns)

    def _bucket(self, path: str) -> str:
        """Which bucket a dotted path falls into."""
        lowered = path.lower()
        leaf = lowered.rsplit(".", 1)[-1]
        for bucket, entries in (
            ("keep", self.keep), ("drop", self.drop), ("hash", self.hash)
        ):
            if lowered in entries or leaf in entries:
                return bucket
        return "default"

    def apply(self, payload: Mapping[str, Any], prefix: str = "") -> (
        dict[str, Any]
    ):
        """Apply the policy to one payload, returning a redacted copy.

        The input is never modified. Fields in no bucket fall through to
        the pattern scrubber, which is the safe direction: an unclassified
        field is still checked for the shapes that must not leave.
        """
        out: dict[str, Any] = {}
        for key, value in payload.items():
            path = f"{prefix}{key}"
            bucket = self._bucket(path)
            if bucket == "drop":
                continue
            if bucket == "hash":
                out[key] = f"sha256:{short_hash(value, 16)}"
                continue
            if bucket == "keep":
                out[key] = value
                continue
            if isinstance(value, Mapping):
                out[key] = self.apply(value, prefix=f"{path}.")
            else:
                out[key] = self.redact(value)
        return out

    def classify(self, path: str) -> str:
        """Which bucket a field is in. Useful in review and in tests."""
        return self._bucket(path)


#: Northstar's policy. Everything a span carries has to be in one of the
#: three buckets or it falls through to the pattern scrubber, and a field
#: falling through is a review finding rather than a default.
REDACTOR = Redactor(
    # Dropped in-process. Never reaches an exporter.
    drop=[
        "messages",
        "tool_result.content",
        "arguments.body",
        "arguments.email",
        "arguments.card_number",
        "prompt",
    ],
    # Correlatable without being readable.
    hash=[
        "arguments.order_id",
        "arguments.customer_id",
        "goal",
    ],
    # Business facts. Needed to reconcile against the ledger.
    keep=[
        "arguments.amount_cents",
        "arguments.currency",
        "arguments.reason",
        "idempotency_key",
        "side_effect.id",
        "policy.decision",
        "budget.remaining_cents",
        "tool",
        "status",
    ],
)
