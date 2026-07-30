"""An in-process A2A transport: the JSON-RPC binding with the sockets removed.

The chapter's claim about the three bindings is that they share one object
model, so switching transports is a deployment change rather than a contract
change. This module is that claim made checkable. It implements the same
card resolution and the same five task operations the HTTP+JSON-RPC binding
implements, and it moves them over a dict lookup instead of a socket.

What that buys is worth naming. The demo is deterministic, needs no
credentials, and cannot be broken by someone else's outage, so a failing
test means the code changed. What it does not buy is conformance: two
implementations agreeing on a specification has never removed the need to
test against the actual peer. Swapping :class:`MockTransport` for an HTTP
one is a change to this file and to nothing in ``client/`` or ``peer/``.

Two hostile conditions are transport-level on purpose, because that is where
they happen in production: :meth:`MockTransport.tamper` serves a modified
card, the way a poisoned CDN cache or a misconfigured static host would, and
:meth:`MockTransport.serve_legacy_card` serves a pre-1.0 document, the way a
peer that has not upgraded would.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit

from peer.adapter import A2AServer
from peer.fraud_review import pre_1_0_card
from wire import AgentCard

__all__ = [
    "WELL_KNOWN_PATH",
    "MockTransport",
    "NoRoute",
    "origin_of",
]

#: Where a peer publishes its card. The entire discovery mechanism: a GET,
#: at a path nobody has to agree on in an email thread.
WELL_KNOWN_PATH = "/.well-known/agent-card.json"


class NoRoute(RuntimeError):
    """Nothing is mounted at that url.

    The in-process analogue of a connection failure, and it is a distinct
    error from a refusal by the peer. A client that cannot tell "nobody
    answered" from "the peer said no" will retry the wrong one.
    """


def origin_of(url: str) -> str:
    """The scheme and host of a url, without the path.

    A card is published at the peer's origin, while task operations go to
    the interface url, which usually has a path. Deriving one from the other
    is what makes a pin that names the endpoint sufficient to find the card.
    """
    parts = urlsplit(url)
    if not parts.scheme or not parts.netloc:
        raise NoRoute(f"{url!r} is not an absolute url")
    return f"{parts.scheme}://{parts.netloc}"


class MockTransport:
    """Card resolution and task operations, in one process.

    Args:
        servers: Interface url to the peer serving it.

    Attributes:
        requests: One record per operation. The demo prints the count to
            show that nothing left the process; a real transport's
            equivalent is an access log.
    """

    def __init__(self, servers: dict[str, A2AServer] | None = None) -> None:
        self._servers: dict[str, A2AServer] = dict(servers or {})
        self._card_overrides: dict[str, tuple[dict[str, Any], str]] = {}
        self.requests: list[dict[str, Any]] = []

    def mount(self, url: str, server: A2AServer) -> MockTransport:
        """Serve ``server`` at ``url``. Returns ``self``, for chaining."""
        self._servers[url] = server
        return self

    # ------------------------------------------------------------ discovery

    def fetch_card(self, url: str) -> AgentCard:
        """GET the peer's card from the origin of ``url``.

        Args:
            url: Either the well-known path or any interface url at the
                peer's origin.

        Returns:
            The parsed card, carrying the detached signature it was served
            with. Note what this method does *not* do: it does not verify
            anything. Fetching is transport; trusting is policy, and it
            lives in :func:`client.resolve.resolve_peer`.

        Raises:
            NoRoute: If nothing is mounted at that origin.
            wire.MalformedCard: If the document is not a v1.0 card.
        """
        origin = origin_of(url)
        self.requests.append({"op": "fetch_card", "origin": origin})
        override = self._card_overrides.get(origin)
        if override is not None:
            body, signature = override
        else:
            body, signature = self._server_at(origin).agent_card()
        return AgentCard.from_dict(body, signature=signature)

    def tamper(self, url: str, changes: dict[str, Any]) -> None:
        """Serve a modified card at ``url``'s origin, keeping the signature.

        This is the misconfigured static host, the poisoned cache, and the
        open redirect, all at once. The document that comes back is
        well-formed, correctly served, and wrong, and TLS has nothing to say
        about it because TLS authenticates the host rather than the claim.

        Args:
            url: Any url at the peer's origin.
            changes: Top-level card fields to overwrite.
        """
        origin = origin_of(url)
        body, signature = self._server_at(origin).agent_card()
        self._card_overrides[origin] = ({**body, **changes}, signature)

    def serve_card(
        self,
        url: str,
        body: dict[str, Any],
        signature: str,
    ) -> None:
        """Serve an arbitrary card body and signature at ``url``'s origin.

        The general form of :meth:`tamper`. Used to build the harder case: a
        card that is *correctly signed* and still not the one anybody
        reviewed, which is what a peer shipping a capability change looks
        like. Only the pinned hash catches that one.
        """
        self._card_overrides[origin_of(url)] = (dict(body), signature)

    def serve_legacy_card(self, url: str) -> None:
        """Serve the pre-1.0 card at ``url``'s origin.

        The peer that did not upgrade. Its document has a top-level ``url``
        and ``protocolVersion``, so a client that reads those fields gets a
        working integration against a protocol version it never tested.
        """
        origin = origin_of(url)
        self._card_overrides[origin] = (pre_1_0_card(), "")

    def clear_overrides(self) -> None:
        """Serve the real card again."""
        self._card_overrides.clear()

    # -------------------------------------------------------- task operations

    def send_task(
        self,
        card: AgentCard,
        delegation: dict[str, Any],
    ) -> dict[str, Any]:
        """Create or rejoin a task at the card's preferred interface.

        The one operation whose idempotency matters, and it is the peer that
        provides it: the same ``(tenant, task_id)`` rejoins rather than
        opening a second review.
        """
        server = self._server_for(card, "send_task")
        return server.send_task(delegation)

    def get_task(
        self,
        card: AgentCard,
        task_id: str,
        *,
        tenant: str,
    ) -> dict[str, Any]:
        """Read one task. A read, so retrying it is free."""
        server = self._server_for(card, "get_task")
        return server.get_task(tenant, task_id)

    def send_message(
        self,
        card: AgentCard,
        task_id: str,
        message: dict[str, Any],
        *,
        tenant: str,
    ) -> dict[str, Any]:
        """Send a message to a non-terminal task."""
        server = self._server_for(card, "send_message")
        return server.send_message(tenant, task_id, message)

    def cancel_task(
        self,
        card: AgentCard,
        task_id: str,
        *,
        tenant: str,
    ) -> dict[str, Any]:
        """Cancel a non-terminal task."""
        server = self._server_for(card, "cancel_task")
        return server.cancel_task(tenant, task_id)

    # ----------------------------------------------------------- internals

    def _server_for(self, card: AgentCard, op: str) -> A2AServer:
        """Route an operation to the card's preferred interface url."""
        url = card.preferred_interface.url
        self.requests.append({"op": op, "url": url})
        server = self._servers.get(url)
        if server is None:
            raise NoRoute(f"nothing is mounted at {url!r}")
        return server

    def _server_at(self, origin: str) -> A2AServer:
        """The server whose interface url sits at ``origin``."""
        for url, server in self._servers.items():
            if origin_of(url) == origin:
                return server
        raise NoRoute(f"nothing is mounted at {origin!r}")
