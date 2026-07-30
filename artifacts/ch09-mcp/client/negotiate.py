"""The two-part admission check: revision *and* capability.

MCP revisions are opaque date strings. There is no major and minor to
compare, no compatibility range to express, and no meaningful "greater
than": a revision is a snapshot of the whole specification, and you compare
it for equality against a set you support. Negotiation then produces one
revision for the entire session, so a feature added after that date is not
degraded, it is absent.

Code that checks only the revision is the most common defect in MCP client
integrations, and its failure mode is silence. The server answers with an
older revision, the call returns successfully, and a capability the
integration assumed was present has been missing for months without one
error. Both halves, or neither.

Refusing the session is the design choice worth arguing about, and it is
the right one. A missing capability discovered at connect time is a config
error with a clear owner. The same missing capability discovered mid-run is
an agent that has already taken four actions and cannot finish the fifth.
"""

from __future__ import annotations

from client.session import McpSession

__all__ = [
    "SUPPORTED",
    "MissingCapability",
    "UnsupportedRevision",
    "negotiate",
]

# Pinned in code, not accepted from whatever a server offers. Most recent
# first: SUPPORTED[0] is what the client asks for.
SUPPORTED = ("2025-11-25", "2025-06-18")


class UnsupportedRevision(Exception):
    """The session landed on a revision this client does not speak."""

    def __init__(self, revision: str) -> None:
        super().__init__(
            f"server negotiated {revision!r}; supported: "
            f"{', '.join(SUPPORTED)}"
        )
        self.revision = revision


class MissingCapability(Exception):
    """The revision is fine and the capability we depend on is not there."""

    def __init__(self, missing: list[str]) -> None:
        super().__init__(f"server does not declare: {', '.join(missing)}")
        self.missing = list(missing)


def negotiate(session: McpSession, need: set[str]) -> str:
    """Agree a revision, or refuse the session at admission.

    Args:
        session: An unopened session. This function opens it.
        need: Capability names this integration depends on.

    Returns:
        The negotiated revision, to record as a span attribute so a trace
        can answer "which revision was this run speaking".

    Raises:
        UnsupportedRevision: The negotiated revision is not in ``SUPPORTED``.
        MissingCapability: The revision is supported and a needed capability
            is not declared.
    """
    hello = session.initialize(protocol_version=SUPPORTED[0])
    if hello.protocol_version not in SUPPORTED:
        raise UnsupportedRevision(hello.protocol_version)
    missing = need - set(hello.capabilities)
    if missing:
        # Fail here, not on turn 40 with money in flight.
        raise MissingCapability(sorted(missing))
    return hello.protocol_version
