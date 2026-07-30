"""Two transports for one server object. Both are in-process mocks.

Nothing here opens a socket, spawns a subprocess, or resolves a hostname.
``StdioPipe`` frames messages the way a real stdio transport does -- one
JSON object per line, in and out -- and hands the line to the server
directly. ``HttpEndpoint`` takes a request object and returns a response
object, and ``Fabric`` routes between origins the way DNS plus a network
would. The mock is the wire, not the protocol: the framing, the status
codes, the ``WWW-Authenticate`` header, the session header, and the
optional upgrade to an event stream are all real, and none of them leaves
the process.

What is deliberately *not* modelled: TLS, connection resumption after a
dropped stream, and the ``Last-Event-ID`` replay that a real Streamable
HTTP server supports. Those are transport engineering, and faking them
here would teach nothing that reading the specification does not.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Protocol
from urllib.parse import urlsplit

from northstar_policy import Principal

from server.auth import (
    READ_SCOPE,
    RESOURCE,
    RESOURCE_METADATA_PATH,
    RESOURCE_METADATA_URL,
    InsufficientScope,
    Unauthorized,
    launch_principal,
    principal_for,
    protected_resource_metadata,
)
from server.readserver import ReadServer

__all__ = [
    "MCP_PATH",
    "Fabric",
    "HttpEndpoint",
    "HttpRequest",
    "HttpResponse",
    "NoRoute",
    "Origin",
    "StdioPipe",
    "www_authenticate",
]

MCP_PATH = "/mcp"

# Streamable HTTP may answer a POST with plain JSON or upgrade that same
# response into an event stream. Northstar upgrades for the one tool that
# can take a while, so a short call stays a short call.
STREAMING_TOOLS = frozenset({"search_orders"})


class NoRoute(Exception):
    """Nothing is mounted at that origin.

    Worth an exception rather than a stub response: it is the assertion
    that this artifact cannot reach anything real.
    """


@dataclass(frozen=True)
class HttpRequest:
    """One request, as an origin sees it."""

    method: str
    path: str
    headers: dict[str, str] = field(default_factory=dict)
    body: Any = None

    def header(self, name: str) -> str | None:
        """Case-insensitive header lookup, as an HTTP server would do."""
        wanted = name.lower()
        for key, value in self.headers.items():
            if key.lower() == wanted:
                return value
        return None


@dataclass(frozen=True)
class HttpResponse:
    """One response. ``events`` non-empty means the SSE upgrade was taken."""

    status: int
    headers: dict[str, str] = field(default_factory=dict)
    body: Any = None
    events: tuple[dict[str, Any], ...] = ()

    def header(self, name: str) -> str | None:
        """Case-insensitive header lookup."""
        wanted = name.lower()
        for key, value in self.headers.items():
            if key.lower() == wanted:
                return value
        return None


class Origin(Protocol):
    """Anything mountable on the fabric."""

    def handle(self, request: HttpRequest) -> HttpResponse:
        """Answer one request."""
        ...


class Fabric:
    """The stand-in for the network: a dict from origin to handler."""

    def __init__(self) -> None:
        self._origins: dict[str, Origin] = {}
        self.log: list[tuple[str, str, int]] = []

    def mount(self, origin: str, handler: Origin) -> Fabric:
        """Make ``handler`` reachable at ``origin``. Returns ``self``."""
        self._origins[origin.rstrip("/")] = handler
        return self

    def fetch(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        body: Any = None,
    ) -> HttpResponse:
        """Route one request by origin.

        Raises:
            NoRoute: If nothing is mounted there. There is no fallback to a
                real network, because there is no real network.
        """
        parts = urlsplit(url)
        origin = f"{parts.scheme}://{parts.netloc}"
        handler = self._origins.get(origin)
        if handler is None:
            raise NoRoute(f"nothing mounted at {origin}")
        response = handler.handle(
            HttpRequest(
                method=method.upper(),
                path=parts.path or "/",
                headers=dict(headers or {}),
                body=body,
            )
        )
        self.log.append((method.upper(), url, response.status))
        return response


class StdioPipe:
    """The stdio transport, in process: newline-delimited JSON both ways.

    A real stdio transport spawns the server as a child process and writes
    to its standard input. That child inherits the parent's environment,
    its filesystem access, and its operating-system identity, which is the
    property that makes stdio the wrong choice for a shared runtime and is
    also exactly what this mock refuses to demonstrate by spawning
    anything. The framing is faithful; the process boundary is not there.

    There is no authorization step, because the transport does not have
    one. The principal comes from the launch.
    """

    def __init__(
        self,
        server: ReadServer,
        principal: Principal | None = None,
    ) -> None:
        self.server = server
        self.principal = principal or launch_principal()
        self.lines_in: list[str] = []

    def send_line(self, line: str) -> str:
        """One JSON object in, one JSON object out. Both newline-framed."""
        self.lines_in.append(line)
        message = json.loads(line)
        response = self.server.handle(message, self.principal)
        return json.dumps(response) + "\n"


class HttpEndpoint:
    """Streamable HTTP: one endpoint, plus the metadata a 401 points at.

    One endpoint is the operational point of the transport. A load
    balancer, a WAF, an authorizing proxy, request logging, and rate limits
    all work on it without knowing what MCP is. This class is where the
    authorization lives, because this is the transport that has any.
    """

    def __init__(self, server: ReadServer) -> None:
        self.server = server
        self.sessions: dict[str, str] = {}
        self._session_seq = 0

    def handle(self, request: HttpRequest) -> HttpResponse:
        """Serve the well-known metadata, or one JSON-RPC POST."""
        if request.path == RESOURCE_METADATA_PATH:
            return HttpResponse(
                200,
                {"Content-Type": "application/json"},
                protected_resource_metadata(),
            )
        if request.path != MCP_PATH:
            return HttpResponse(404, body={"error": "not_found"})
        if request.method != "POST":
            return HttpResponse(405, body={"error": "method_not_allowed"})

        header = request.header("Authorization") or ""
        if not header.startswith("Bearer "):
            return self._challenge(
                401, "invalid_request", "no bearer token presented"
            )

        token = header[len("Bearer "):].strip()
        try:
            principal = principal_for(token, READ_SCOPE)
        except InsufficientScope as exc:
            # 403 and a named scope, not a flat denial: this is the signal
            # a client needs to run an incremental authorization.
            return self._challenge(
                403, "insufficient_scope", str(exc), scope=exc.required
            )
        except Unauthorized as exc:
            return self._challenge(401, "invalid_token", exc.reason)

        # A session id is not an authentication credential. Bind it to the
        # authenticated principal, or session hijacking is available to
        # anyone who can read the header off a log line.
        session_id = request.header("Mcp-Session-Id")
        if session_id is not None:
            owner = self.sessions.get(session_id)
            if owner is None:
                return self._challenge(401, "invalid_token", "unknown session")
            if owner != principal.user_id:
                return self._challenge(
                    401, "invalid_token", "session bound to another principal"
                )

        message = request.body if isinstance(request.body, dict) else {}
        response = self.server.handle(message, principal)

        headers = {"Content-Type": "application/json"}
        if message.get("method") == "initialize":
            self._session_seq += 1
            new_id = f"sess-{self._session_seq:04d}"
            self.sessions[new_id] = principal.user_id or ""
            headers["Mcp-Session-Id"] = new_id

        tool = (message.get("params") or {}).get("name")
        if message.get("method") == "tools/call" and tool in STREAMING_TOOLS:
            # Same response, upgraded: a progress notification first, the
            # result last. A client that only knows plain JSON never asked
            # for this path, which is why the upgrade is per-response.
            headers["Content-Type"] = "text/event-stream"
            return HttpResponse(200, headers, None, (
                {"jsonrpc": "2.0", "method": "notifications/progress",
                 "params": {"progress": 0.5, "message": f"scanning {tool}"}},
                response,
            ))
        return HttpResponse(200, headers, response)

    def _challenge(
        self,
        status: int,
        error: str,
        description: str,
        scope: str | None = None,
    ) -> HttpResponse:
        """Refuse, and say where to go to stop being refused."""
        return HttpResponse(
            status,
            {
                "WWW-Authenticate": www_authenticate(error, description, scope),
                "Content-Type": "application/json",
            },
            {"error": error, "error_description": description},
        )


def www_authenticate(
    error: str,
    description: str,
    scope: str | None = None,
) -> str:
    """Build the header that makes client configuration unnecessary.

    The ``resource_metadata`` parameter is the whole discovery mechanism: a
    client that has never heard of Northstar learns, from a rejection, the
    resource identifier to bind a token to and which authorization servers
    can mint one.
    """
    parts = [
        f'Bearer resource_metadata="{RESOURCE_METADATA_URL}"',
        f'resource="{RESOURCE}"',
        f'error="{error}"',
        f'error_description="{description}"',
    ]
    if scope is not None:
        parts.append(f'scope="{scope}"')
    return ", ".join(parts)
