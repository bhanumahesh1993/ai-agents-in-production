"""Field-level redaction, applied before anything leaves the process.

Agent traces are unusually rich and unusually dangerous. A tool span
carries the arguments the model chose, which is exactly what makes the
trace worth having and exactly what makes it a data-protection problem: the
customer's email address, the body of the message you are about to send
them, sometimes a token.

Two rules the book insists on:

1. **Redact at the boundary, not at the dashboard.** A payload that reaches
   your observability vendor is a payload you have disclosed, whatever the
   dashboard chooses to display.
2. **Redact by field name first, pattern second.** Field names are
   deterministic. Patterns catch the things that end up in a free-text
   field despite everyone's best intentions.

Redaction is one-way. When you need to correlate without seeing, use
``hash_values=True``: identical inputs produce identical tokens, so you can
still count distinct customers without learning who they are.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

__all__ = ["DEFAULT_PATTERNS", "DEFAULT_SENSITIVE_FIELDS", "Redactor"]

#: Field names redacted by default, matched case-insensitively against the
#: last path segment. Add your own; this list is a starting point and not a
#: compliance artifact.
DEFAULT_SENSITIVE_FIELDS: frozenset[str] = frozenset(
    {
        "access_token",
        "address",
        "api_key",
        "authorization",
        "body",
        "card_number",
        "credential",
        "email",
        "password",
        "phone",
        "refresh_token",
        "secret",
        "ssn",
        "token",
    }
)

#: Patterns applied to every string value that survives field redaction.
DEFAULT_PATTERNS: tuple[tuple[str, str], ...] = (
    ("email", r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"),
    ("card", r"\b(?:\d[ -]?){13,19}\b"),
    ("bearer", r"(?i)bearer\s+[A-Za-z0-9._~+/-]{8,}=*"),
    ("api_key", r"\b(?:sk|pk|api)[-_][A-Za-z0-9]{12,}\b"),
)


class Redactor:
    """Walks a structure and removes what should not leave the process.

    Args:
        fields: Field names to redact wholesale.
        patterns: ``(label, regex)`` pairs applied to string values.
        replacement: What a redacted value becomes.
        hash_values: Emit ``redacted:<8 hex chars>`` instead of a constant,
            so equal values stay equal and can still be grouped.
        max_depth: Guard against pathological nesting.

    Example:
        >>> r = Redactor.default()
        >>> r.redact({"order_id": "NR-1", "email": "ada@example.com"})
        {'order_id': 'NR-1', 'email': '[redacted]'}
        >>> r.redact("write to ada@example.com please")
        'write to [redacted:email] please'
    """

    def __init__(
        self,
        fields: Iterable[str] = (),
        patterns: Sequence[tuple[str, str]] = (),
        *,
        replacement: str = "[redacted]",
        hash_values: bool = False,
        max_depth: int = 12,
    ) -> None:
        self.fields = {f.lower() for f in fields}
        self.patterns = [
            (label, re.compile(pattern)) for label, pattern in patterns
        ]
        self.replacement = replacement
        self.hash_values = hash_values
        self.max_depth = max_depth
        self.redactions = 0

    @classmethod
    def default(cls, **kwargs: Any) -> Redactor:
        """A redactor with the default field list and patterns."""
        return cls(DEFAULT_SENSITIVE_FIELDS, DEFAULT_PATTERNS, **kwargs)

    @classmethod
    def off(cls) -> Redactor:
        """A redactor that redacts nothing.

        Legitimate in local development against synthetic fixtures. Not
        legitimate anywhere a real customer's data can reach it.
        """
        return cls()

    def redact(self, value: Any, _depth: int = 0) -> Any:
        """Return a redacted copy of ``value``.

        The input is never modified. Unknown types are passed through
        unchanged, which is the safe direction for numbers and booleans
        and the risky one for custom objects — convert those to dicts
        before they reach here.
        """
        if _depth > self.max_depth:
            return self.replacement
        if isinstance(value, Mapping):
            return {
                key: (
                    self._mask(value[key])
                    if str(key).lower() in self.fields
                    else self.redact(value[key], _depth + 1)
                )
                for key in value
            }
        if isinstance(value, list | tuple):
            redacted = [self.redact(item, _depth + 1) for item in value]
            return type(value)(redacted) if isinstance(value, tuple) else redacted
        if isinstance(value, str):
            return self._scrub(value)
        return value

    def redact_event(self, record: Mapping[str, Any]) -> dict[str, Any]:
        """Redact one event-log record, leaving its envelope intact.

        ``run_id``, ``step``, ``type``, and ``ts`` are never touched: they
        are the fields you need in order to find the record you are looking
        for, and none of them is customer data.
        """
        return {
            "run_id": record.get("run_id"),
            "step": record.get("step"),
            "type": record.get("type"),
            "ts": record.get("ts"),
            "payload": self.redact(record.get("payload") or {}),
        }

    def _mask(self, value: Any) -> str:
        """Replace one value entirely."""
        self.redactions += 1
        if not self.hash_values:
            return self.replacement
        digest = hashlib.sha256(str(value).encode("utf-8")).hexdigest()
        return f"redacted:{digest[:8]}"

    def _scrub(self, text: str) -> str:
        """Apply the patterns to one string."""
        out = text
        for label, pattern in self.patterns:
            replacement = (
                self.replacement
                if self.replacement != "[redacted]"
                else f"[redacted:{label}]"
            )
            out, count = pattern.subn(replacement, out)
            self.redactions += count
        return out
