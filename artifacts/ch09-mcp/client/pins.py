"""The tool surface as a supply-chain artifact: hash it, pin it, diff it.

The hash covers exactly the text that reaches the model -- name,
description, and input schema -- computed over a canonical form so that key
ordering and whitespace do not raise a false alarm. ``outputSchema`` is
left out on purpose: the model never reads it, so a server that adds a
field to its declared output has not changed what the model was told.

OWASP's 2026 agentic top ten files this under ASI04, agentic supply chain
vulnerabilities, and its recommended controls are inventory, pinning,
signing, scanning, an allowlist registry, and provenance. That is a
supply-chain programme, not a prompt fix, and this module is the smallest
useful piece of one: a pin, checked on every connection, that fails the
session rather than the turn.
"""

from __future__ import annotations

import hashlib
import json

from northstar_contracts import short_hash

__all__ = [
    "PINNED_TOOLS",
    "PINS",
    "PinMismatch",
    "check_pin",
    "diff_against_pin",
    "surface_hash",
    "tool_hashes",
]


def surface_hash(tools: list[dict]) -> str:
    """Hash the exact text the model will read."""
    fields = ("name", "description", "inputSchema")
    canonical = json.dumps(
        [{k: t[k] for k in fields}
         for t in sorted(tools, key=lambda t: t["name"])],
        sort_keys=True, separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode()).hexdigest()[:32]


def tool_hashes(tools: list[dict]) -> dict[str, str]:
    """Per-tool digests, so a mismatch can name which tool moved."""
    fields = ("name", "description", "inputSchema")
    return {
        t["name"]: short_hash({k: t[k] for k in fields}, 16)
        for t in tools
    }


# Pinned at review time. A mismatch fails the session, not a turn.
PINS = {
    "northstar-reads": "f2785ec17fa205f6ff9bd490b48a187c",
    "vendor-catalog": "3dc53ac367598152ad56c40d92832432",
}

# The same pin, one level finer, so the failure report says *what* changed
# instead of only *that* something did. Storing per-tool digests rather
# than the full text keeps the review artifact small and still produces a
# diff a human can act on.
PINNED_TOOLS = {
    "northstar-reads": {
        "get_order": "be6224e1e79d2605",
        "get_policy": "01c5f1d3c6a4a95a",
        "search_orders": "941336349b02d590",
    },
    "vendor-catalog": {
        "lookup_catalog_item": "d8f6887762b0d1c1",
    },
}


class PinMismatch(Exception):
    """The advertised tool surface is not the one that was reviewed."""

    def __init__(
        self,
        server: str,
        expected: str,
        actual: str,
        diff: list[str],
    ) -> None:
        super().__init__(
            f"{server}: tool surface {actual} does not match pin {expected}"
        )
        self.server = server
        self.expected = expected
        self.actual = actual
        self.diff = list(diff)


def diff_against_pin(server: str, tools: list[dict]) -> list[str]:
    """Describe how this surface differs from the one that was reviewed.

    Args:
        server: The pinned server name.
        tools: The descriptors this connection was served.

    Returns:
        One line per difference, empty when the surface matches.
    """
    pinned = PINNED_TOOLS.get(server, {})
    live = tool_hashes(tools)
    lines: list[str] = []
    for name in sorted(set(pinned) | set(live)):
        if name not in live:
            lines.append(f"- removed: {name}")
        elif name not in pinned:
            lines.append(f"+ added:   {name} ({live[name]})")
        elif pinned[name] != live[name]:
            lines.append(
                f"~ changed: {name} {pinned[name]} -> {live[name]}"
            )
    return lines


def check_pin(server: str, tools: list[dict]) -> str:
    """Fail the session when the surface has moved.

    Args:
        server: The pinned server name.
        tools: The descriptors this connection was served.

    Returns:
        The surface hash, to record on the session's span.

    Raises:
        PinMismatch: If the server is unpinned, or its surface has changed.
            An unpinned server is a mismatch, not an exemption: connecting
            by discovery alone is how the count goes from three to nine.
    """
    actual = surface_hash(tools)
    expected = PINS.get(server)
    if expected is None:
        raise PinMismatch(server, "(unpinned)", actual,
                          [f"+ added:   server {server}"])
    if expected != actual:
        raise PinMismatch(server, expected, actual,
                          diff_against_pin(server, tools))
    return actual
