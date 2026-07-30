"""The MCP server object: JSON-RPC in, JSON-RPC out, no transport.

MCP separates the protocol from the pipe. This class is the protocol half.
It knows ``initialize``, ``tools/list`` and ``tools/call``, it renders its
tool surface from one ``ToolRegistry``, and it has never heard of a socket,
a pipe, or a header. ``server.transports`` supplies the pipes, and the same
object instance can be rendered over both at once -- which is what makes
"do the two transports agree?" a question the demo can actually answer.

Authorization does not live here either, because it cannot: stdio has no
authorization step at all. The transport establishes the principal (from a
validated token over HTTP, from the launch over stdio) and hands it in. The
server still checks the scope before dispatching, because a control that
only exists in one of two code paths is a control that is one refactor from
being absent.
"""

from __future__ import annotations

import copy
from typing import Any

from northstar_contracts import ToolCall, canonical_json
from northstar_policy import Principal
from northstar_runtime import ToolRegistry

from server.auth import READ_SCOPE
from server.expose import descriptors

__all__ = [
    "CURRENT_REVISION",
    "DEFAULT_CAPABILITIES",
    "PREVIOUS_REVISION",
    "ReadServer",
]

# Revisions are opaque date strings. There is no major, no minor, and no
# meaningful "greater than": you compare them for equality against a set.
CURRENT_REVISION = "2025-11-25"
PREVIOUS_REVISION = "2025-06-18"

# Capabilities are negotiated separately from the revision. A server on the
# current revision may still not offer resources, elicitation, or
# subscriptions, which is why a client has to check both.
DEFAULT_CAPABILITIES: dict[str, Any] = {
    "tools": {"listChanged": True},
    "resources": {"subscribe": True, "listChanged": True},
    "logging": {},
}

# JSON-RPC error codes. -32001 is the server-defined range.
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
FORBIDDEN = -32001


class ReadServer:
    """Northstar's read-only MCP server.

    Args:
        registry: The single source of truth for the tool surface.
        name: The server name a client pins its tool surface against.
        revision: The protocol revision this server will actually speak.
        capabilities: What it declares at ``initialize``.

    Raises:
        ConfigError: If the registry holds a tool that writes. The server
            refuses to start rather than refusing one call later.
    """

    def __init__(
        self,
        registry: ToolRegistry,
        *,
        name: str = "northstar-reads",
        revision: str = CURRENT_REVISION,
        capabilities: dict[str, Any] | None = None,
    ) -> None:
        self.name = name
        self.revision = revision
        self.capabilities = copy.deepcopy(
            DEFAULT_CAPABILITIES if capabilities is None else capabilities
        )
        self.registry = registry
        self._descriptors = descriptors(registry)
        self.connections = 0

    # ------------------------------------------------------------ the surface

    def tool_descriptors(self) -> list[dict]:
        """The tool surface as this connection sees it right now.

        A method rather than an attribute because a tool surface is a live
        feed from a party that may not be you, not a static artifact you
        reviewed once. ``server.drift`` overrides exactly this.
        """
        return copy.deepcopy(self._descriptors)

    # ------------------------------------------------------------- dispatch

    def handle(
        self,
        message: dict[str, Any],
        principal: Principal,
    ) -> dict[str, Any]:
        """Answer one JSON-RPC request.

        Args:
            message: A JSON-RPC 2.0 request object.
            principal: Who the transport says is calling.

        Returns:
            A JSON-RPC 2.0 response object.
        """
        method = str(message.get("method", ""))
        params: dict[str, Any] = message.get("params") or {}
        request_id = message.get("id")

        if method == "initialize":
            return self._ok(request_id, self._initialize(params))
        if method == "tools/list":
            return self._ok(request_id, {"tools": self.tool_descriptors()})
        if method == "tools/call":
            return self._call(request_id, params, principal)
        return self._error(
            request_id, METHOD_NOT_FOUND, f"unknown method {method!r}"
        )

    def _initialize(self, params: dict[str, Any]) -> dict[str, Any]:
        """Negotiate one revision for the whole session.

        If the client asked for a revision this server speaks, echo it. If
        not, answer with the one this server does speak and let the client
        decide whether to continue on those terms or disconnect. There is no
        per-feature fallback: features added after the agreed date are
        simply absent.
        """
        self.connections += 1
        asked = str(params.get("protocolVersion", ""))
        agreed = asked if asked == self.revision else self.revision
        return {
            "protocolVersion": agreed,
            "capabilities": copy.deepcopy(self.capabilities),
            "serverInfo": {"name": self.name, "version": "1.0.0"},
        }

    def _call(
        self,
        request_id: Any,
        params: dict[str, Any],
        principal: Principal,
    ) -> dict[str, Any]:
        """Run one tool, once the principal is known to hold the scope."""
        if not principal.has(READ_SCOPE):
            return self._error(
                request_id,
                FORBIDDEN,
                f"requires scope {READ_SCOPE!r}",
                data={"error": "insufficient_scope", "scope": READ_SCOPE},
            )

        name = str(params.get("name", ""))
        if self.registry.spec_for(name) is None:
            return self._error(
                request_id,
                INVALID_PARAMS,
                f"no tool named {name!r} on {self.name}",
                data={"tools": self.registry.names()},
            )

        arguments = dict(params.get("arguments") or {})
        result = self.registry.dispatch(
            ToolCall(id=f"{self.name}-{request_id}", name=name,
                     arguments=arguments)
        )
        return self._ok(request_id, {
            # Text content is what an older client reads; structuredContent
            # is the 2025-06-18 addition, and it is the one a program
            # should parse. Both carry the same value.
            "content": [{"type": "text",
                         "text": canonical_json(result.content)}],
            "structuredContent": result.content,
            "isError": not result.ok,
        })

    # -------------------------------------------------------------- plumbing

    @staticmethod
    def _ok(request_id: Any, result: dict[str, Any]) -> dict[str, Any]:
        """A JSON-RPC success response."""
        return {"jsonrpc": "2.0", "id": request_id, "result": result}

    @staticmethod
    def _error(
        request_id: Any,
        code: int,
        message: str,
        data: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """A JSON-RPC error response."""
        error: dict[str, Any] = {"code": code, "message": message}
        if data is not None:
            error["data"] = data
        return {"jsonrpc": "2.0", "id": request_id, "error": error}
