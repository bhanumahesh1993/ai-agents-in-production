"""One MCP connection, over either transport, plus the authorization walk.

``McpSession`` speaks JSON-RPC and nothing else. Which pipe carries it is a
constructor argument, which is the property that lets the demo render one
server object over both transports and compare the two answers byte for
byte.

``HttpTransport`` is where the interesting part is. It starts with no
configuration at all -- no client id for the identity provider, no token
endpoint, no idea who issues tokens for Northstar -- makes the call anyway,
and learns everything it needs from being refused:

    POST /mcp                       -> 401 + WWW-Authenticate
    GET  /.well-known/oauth-protected-resource
                                    -> resource id, authorization servers
    GET  <issuer>/.well-known/oauth-authorization-server
                                    -> token endpoint
    POST <token endpoint>           -> an audience-bound access token
    POST /mcp  (with the token)     -> 200

Two checks in that walk are not decorative. The token response's ``iss``
must match the issuer discovery started from (RFC 9207), or an attacker who
can shape a discovery document sends the client to an authorization server
they control. And the token is requested *for* the resource identifier the
metadata named, so what comes back cannot be replayed elsewhere.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Protocol

from server.auth import READ_SCOPE
from server.transports import Fabric, HttpResponse, StdioPipe

__all__ = [
    "Hello",
    "HttpRefused",
    "HttpTransport",
    "IssuerMixup",
    "McpSession",
    "RpcError",
    "StdioTransport",
    "Transport",
]

AS_METADATA_PATH = "/.well-known/oauth-authorization-server"


class RpcError(Exception):
    """The server answered with a JSON-RPC error object."""

    def __init__(self, code: int, message: str, data: Any = None) -> None:
        super().__init__(f"[{code}] {message}")
        self.code = code
        self.message = message
        self.data = data


class HttpRefused(Exception):
    """The transport refused before any JSON-RPC was exchanged."""

    def __init__(self, response: HttpResponse) -> None:
        challenge = response.header("WWW-Authenticate") or ""
        super().__init__(f"HTTP {response.status}: {challenge}")
        self.status = response.status
        self.challenge = challenge
        self.body = response.body


class IssuerMixup(Exception):
    """Discovery led somewhere the issuer did not vouch for.

    RFC 9207, brought into MCP by SEP-2468. Without this check the mix-up
    attack is available to any party in the chain that can shape a
    discovery document.
    """


@dataclass(frozen=True)
class Hello:
    """What ``initialize`` came back with.

    ``capabilities`` stays a dict so that ``set(hello.capabilities)`` reads
    the declared capability names, which is what the admission check needs,
    while the nested detail is still there for a span attribute.
    """

    protocol_version: str
    capabilities: dict[str, Any] = field(default_factory=dict)
    server_name: str = ""


class Transport(Protocol):
    """Anything that can carry one JSON-RPC message and bring one back."""

    def request(self, message: dict[str, Any]) -> dict[str, Any]:
        """Send one request object, return one response object."""
        ...


class StdioTransport:
    """The client end of a stdio pipe.

    One JSON object per line, in and out. No authorization step: the trust
    model is that this process launched that process, and everything the
    launching process holds went with it.
    """

    name = "stdio"

    def __init__(self, pipe: StdioPipe) -> None:
        self.pipe = pipe

    def request(self, message: dict[str, Any]) -> dict[str, Any]:
        """Frame, send, and parse one message."""
        line = self.pipe.send_line(json.dumps(message) + "\n")
        return json.loads(line)


class HttpTransport:
    """The client end of a Streamable HTTP endpoint, with the OAuth walk.

    Args:
        fabric: The stand-in for the network.
        url: The single MCP endpoint.
        client_id: A Client ID Metadata Document URL. The authorization
            server resolves it instead of storing a registration.
        subject: The human the work is being done for.
        scope: The scope to request. Set it to something narrower than the
            call needs to see a 403 and a step-up hint instead of a result.
        token: Pre-supplied token, for the negative cases. When set, the
            discovery walk does not run.
        discover: Set ``False`` to make the unauthenticated call and stop
            at the 401, which is what ``./run.sh http --no-token`` does.
    """

    name = "http"

    def __init__(
        self,
        fabric: Fabric,
        url: str,
        *,
        client_id: str = "https://apps.northstar.example/clients/support",
        subject: str = "CUST-8841",
        scope: str = READ_SCOPE,
        token: str | None = None,
        discover: bool = True,
    ) -> None:
        self.fabric = fabric
        self.url = url
        self.client_id = client_id
        self.subject = subject
        self.scope = scope
        self.token = token
        self.discover = discover
        self.session_id: str | None = None
        self.walk: list[str] = []
        self.last_challenge: str = ""
        self.streamed: list[dict[str, Any]] = []

    def request(self, message: dict[str, Any]) -> dict[str, Any]:
        """Send one message, acquiring a token first if that is what it takes.

        Raises:
            HttpRefused: On any status the walk cannot resolve, which is
                every status once a token has been presented.
        """
        response = self._post(message)
        if response.status == 401 and self.token is None and self.discover:
            self.last_challenge = response.header("WWW-Authenticate") or ""
            self._acquire_token(self.last_challenge)
            response = self._post(message)

        if response.status != 200:
            self.last_challenge = response.header("WWW-Authenticate") or ""
            raise HttpRefused(response)

        session_id = response.header("Mcp-Session-Id")
        if session_id is not None:
            self.session_id = session_id

        if response.events:
            # The response upgraded to an event stream. Notifications
            # first, the reply last: the reply is the message with an id.
            replies = [e for e in response.events if "id" in e]
            self.streamed.extend(
                e for e in response.events if "id" not in e
            )
            return replies[-1]
        body = response.body
        return body if isinstance(body, dict) else {}

    def _post(self, message: dict[str, Any]) -> HttpResponse:
        """One POST to the single endpoint, with whatever we currently hold."""
        headers: dict[str, str] = {"Content-Type": "application/json"}
        if self.token is not None:
            headers["Authorization"] = f"Bearer {self.token}"
        if self.session_id is not None:
            headers["Mcp-Session-Id"] = self.session_id
        return self.fabric.fetch("POST", self.url, headers=headers,
                                 body=message)

    def _acquire_token(self, challenge: str) -> None:
        """Walk from a 401 to an audience-bound token.

        Raises:
            HttpRefused: If any step of discovery does not answer.
            IssuerMixup: If the token did not come from the issuer that
                discovery started at.
        """
        metadata_url = _challenge_param(challenge, "resource_metadata")
        if not metadata_url:
            raise IssuerMixup("401 carried no resource_metadata pointer")
        self.walk.append(f"401 -> {metadata_url}")

        prm = self._json("GET", metadata_url)
        resource = str(prm["resource"])
        issuer = str(prm["authorization_servers"][0])
        self.walk.append(f"resource={resource} issuer={issuer}")

        as_metadata = self._json("GET", f"{issuer}{AS_METADATA_PATH}")
        if as_metadata.get("issuer") != issuer:
            raise IssuerMixup(
                f"metadata at {issuer} claims issuer "
                f"{as_metadata.get('issuer')!r}"
            )
        token_endpoint = str(as_metadata["token_endpoint"])
        self.walk.append(f"token_endpoint={token_endpoint}")

        granted = self._json("POST", token_endpoint, body={
            "grant_type": "authorization_code",
            "client_id": self.client_id,
            "subject": self.subject,
            # RFC 8707. Ask for a token for this resource and nothing else.
            "resource": resource,
            "scope": self.scope,
        })
        if granted.get("iss") != issuer:
            raise IssuerMixup(
                f"token response from {granted.get('iss')!r}, expected "
                f"{issuer!r}"
            )
        self.token = str(granted["access_token"])
        self.walk.append(f"token for aud={resource} scope={granted['scope']}")

    def _json(
        self,
        method: str,
        url: str,
        body: Any = None,
    ) -> dict[str, Any]:
        """Fetch one JSON document, or refuse the walk."""
        response = self.fabric.fetch(method, url, body=body)
        if response.status != 200 or not isinstance(response.body, dict):
            raise HttpRefused(response)
        return response.body


class McpSession:
    """One client session. Knows JSON-RPC; knows nothing about the pipe."""

    def __init__(self, transport: Transport, server_name: str = "") -> None:
        self.transport = transport
        self.server_name = server_name
        self.protocol_version: str = ""
        self.capabilities: dict[str, Any] = {}
        self._seq = 0

    def initialize(self, protocol_version: str) -> Hello:
        """Open the session and record what was agreed.

        Args:
            protocol_version: The revision the client prefers.

        Returns:
            The server's answer. Whether it is acceptable is
            ``client.negotiate``'s decision, not this method's.
        """
        result = self._rpc("initialize", {
            "protocolVersion": protocol_version,
            "capabilities": {"roots": {"listChanged": True}},
            "clientInfo": {"name": "northstar-support-client",
                           "version": "1.0.0"},
        })
        self.protocol_version = str(result.get("protocolVersion", ""))
        self.capabilities = dict(result.get("capabilities") or {})
        server_info = result.get("serverInfo") or {}
        self.server_name = str(server_info.get("name", self.server_name))
        return Hello(
            protocol_version=self.protocol_version,
            capabilities=self.capabilities,
            server_name=self.server_name,
        )

    def list_tools(self) -> list[dict]:
        """The advertised tool surface, exactly as the model would read it."""
        return list(self._rpc("tools/list", {}).get("tools", []))

    def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        """Call one tool and return its structured content.

        Raises:
            RpcError: If the server refused the call outright.
        """
        result = self._rpc("tools/call", {"name": name,
                                          "arguments": arguments})
        if result.get("isError"):
            raise RpcError(0, f"{name} failed", result.get("structuredContent"))
        return result.get("structuredContent")

    def _rpc(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        """One request/response pair.

        Raises:
            RpcError: On a JSON-RPC error object.
        """
        self._seq += 1
        response = self.transport.request({
            "jsonrpc": "2.0",
            "id": self._seq,
            "method": method,
            "params": params,
        })
        if "error" in response:
            error = response["error"]
            raise RpcError(int(error.get("code", 0)),
                           str(error.get("message", "")),
                           error.get("data"))
        result = response.get("result")
        return result if isinstance(result, dict) else {}


def _challenge_param(challenge: str, name: str) -> str:
    """Pull one quoted parameter out of a ``WWW-Authenticate`` header."""
    match = re.search(rf'{name}="([^"]*)"', challenge)
    return match.group(1) if match else ""
