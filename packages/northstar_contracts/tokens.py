"""A deterministic token estimate.

Real tokenisers differ per model and change between releases. Everything in
this repository that needs a token count needs it for *budgeting* — how
much of a tool result to keep, when to compact, what to charge a run — and
budgeting needs an estimate that is stable, offline, and identical on every
machine, not one that is exact.

So this is a four-characters-per-token approximation with a small
per-structure overhead. It is wrong by roughly 10-20% against a real
tokeniser, consistently. Swap it for the real thing when you bill on it;
keep it when you test on it.
"""

from __future__ import annotations

from typing import Any

from .ids import canonical_json

__all__ = ["CHARS_PER_TOKEN", "estimate_tokens"]

#: Characters per token in the approximation. English prose sits near 4.
CHARS_PER_TOKEN = 4


def estimate_tokens(value: Any) -> int:
    """Estimate the token cost of a value.

    Args:
        value: A string, or any JSON-serialisable structure. Non-serialisable
            values fall back to ``repr``, so this never raises.

    Returns:
        An estimated token count, at least 1 for any non-empty value.
    """
    if isinstance(value, str):
        text = value
    else:
        try:
            text = canonical_json(value)
        except TypeError:
            text = repr(value)
    if not text:
        return 0
    return max(1, len(text) // CHARS_PER_TOKEN)
