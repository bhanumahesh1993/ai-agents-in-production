"""The local MCP tool gateway: six Northstar tools, stdio and in-process.

The gateway exists locally for a specific reason. It is where policy
evaluation and identity live, and running the agent against tools it
imported as Python functions would let you develop for weeks against an
authorization boundary that does not exist. The first time policy actually
evaluates would then be in an environment where a denial is an incident.

So this is a real Model Context Protocol server: JSON-RPC 2.0 over stdio,
with ``initialize``, ``tools/list``, and ``tools/call``. It runs offline,
it holds the world, and it evaluates the **shared** policy bundle before
any tool executes. :class:`GatewayRegistry` speaks to it in process, so the
agent's tool calls cross the protocol boundary without a socket.

Run it as a server::

    python artifacts/ch21-local/mcp_server.py    # stdio, one JSON per line
"""

from __future__ import annotations

import json
import sys
from collections.abc import Iterable
from typing import Any, TextIO

from northstar_contracts import ToolCall, ToolResult, ToolSpec, World
from northstar_policy import (
    Decision,
    PolicyEngine,
    Principal,
    default_northstar_policy,
)
from northstar_runtime import ToolRegistry

__all__ = [
    "JSONRPC_VERSION",
    "PROTOCOL_REVISION",
    "SUPPORT_PRINCIPAL",
    "GatewayRegistry",
    "MCPServer",
    "registry_for",
    "serve_stdio",
]

JSONRPC_VERSION = "2.0"

#: The specification revision this gateway implements. Pinned rather than
#: floating: a protocol revision is a contract, and ``VERSIONS.md`` records
#: which one this edition was verified against.
PROTOCOL_REVISION = "2025-11-25"

#: The principal every local run acts as. Note the scope spelling: these
#: are the shared bundle's names, because the policy bundle is on the
#: must-be-identical list and a local fork of it makes a green local run
#: mean nothing.
SUPPORT_PRINCIPAL = Principal(
    user_id="CUST-8841",
    agent_id="northstar-support-agent",
    operator_id="northstar-platform",
    scopes=frozenset({"orders:read", "refunds:write"}),
)

# JSON-RPC error codes. -32000 upward is the implementation-defined range.
_METHOD_NOT_FOUND = -32601
_INVALID_PARAMS = -32602
_NOT_AUTHORIZED = -32001


class MCPServer:
    """Six Northstar tools behind a policy-evaluating MCP endpoint.

    Args:
        world: The authoritative store the tools act on.
        policy: The decision point. Defaults to the shared bundle.
        principal: Who calls arrive as.

    Example:
        >>> server = MCPServer(World())
        >>> reply = server.handle({"jsonrpc": "2.0", "id": 1,
        ...                        "method": "tools/list"})
        >>> len(reply["result"]["tools"])
        6
    """

    def __init__(
        self,
        world: World,
        policy: PolicyEngine | None = None,
        principal: Principal = SUPPORT_PRINCIPAL,
    ) -> None:
        self.world = world
        self.policy = policy or default_northstar_policy()
        self.principal = principal
        self.registry = ToolRegistry(
            inject_idempotency_key=True
        ).register_all(world.tools())
        #: Every call the gateway saw, with the decision it made. This is
        #: the record a local run produces that an imported function never
        #: would.
        self.calls: list[dict[str, Any]] = []

    # ------------------------------------------------------------ protocol

    def handle(self, request: dict[str, Any]) -> dict[str, Any]:
        """Handle one JSON-RPC request and return one response."""
        method = request.get("method", "")
        request_id = request.get("id")
        params = request.get("params") or {}
        if method == "initialize":
            return self._ok(request_id, self._initialize())
        if method == "tools/list":
            return self._ok(
                request_id,
                {"tools": [self._describe(s) for s in self.registry.specs()]},
            )
        if method == "tools/call":
            return self._call(request_id, params)
        return self._error(
            request_id, _METHOD_NOT_FOUND, f"unknown method {method!r}"
        )

    def _initialize(self) -> dict[str, Any]:
        """What the server tells a client about itself."""
        return {
            "protocolVersion": PROTOCOL_REVISION,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": "northstar-gateway", "version": "1.0.0"},
        }

    @staticmethod
    def _describe(spec: ToolSpec) -> dict[str, Any]:
        """Render one tool contract in the shape MCP clients expect."""
        return {
            "name": spec.name,
            "description": spec.description,
            "inputSchema": spec.input_schema,
            "annotations": {
                "readOnlyHint": not spec.writes,
                "idempotentHint": spec.idempotent,
                "version": spec.version,
            },
        }

    def _call(
        self,
        request_id: Any,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        """Authorise, then execute. Never the other way round."""
        name = str(params.get("name", ""))
        arguments = dict(params.get("arguments") or {})
        run_id = str(params.get("_meta", {}).get("run_id", "local"))
        step = int(params.get("_meta", {}).get("step", 0))
        call = ToolCall(str(params.get("id", "mcp")), name, arguments)

        if self.registry.spec_for(name) is None:
            return self._error(
                request_id, _INVALID_PARAMS, f"no tool named {name!r}"
            )

        decision = self.policy.evaluate(
            self.principal, call, {"run_id": run_id, "step": step}
        )
        self.calls.append(
            {
                "tool": name,
                "decision": decision.value,
                "principal": self.principal.to_dict(),
                "run_id": run_id,
                "step": step,
            }
        )
        if decision is not Decision.ALLOW:
            return self._error(
                request_id,
                _NOT_AUTHORIZED,
                f"{name} is {decision.value} for this principal",
            )

        result = self.registry.dispatch(call, run_id=run_id, step=step)
        return self._ok(
            request_id,
            {
                "content": [
                    {"type": "text", "text": json.dumps(result.content)}
                ],
                "structuredContent": result.content,
                "isError": not result.ok,
            },
        )

    @staticmethod
    def _ok(request_id: Any, result: dict[str, Any]) -> dict[str, Any]:
        """A JSON-RPC success response."""
        return {"jsonrpc": JSONRPC_VERSION, "id": request_id,
                "result": result}

    @staticmethod
    def _error(request_id: Any, code: int, message: str) -> dict[str, Any]:
        """A JSON-RPC error response."""
        return {
            "jsonrpc": JSONRPC_VERSION,
            "id": request_id,
            "error": {"code": code, "message": message},
        }


class GatewayRegistry(ToolRegistry):
    """A registry whose dispatch goes over MCP instead of into a function.

    The specs come from ``tools/list`` and the calls go through
    ``tools/call``, so the agent reaches its tools through the same
    protocol boundary it will use in production. Nothing in the loop knows
    the difference, which is the property worth having.

    Args:
        server: The gateway. In production this is a URL; here it is an
            object, and the transport is the only thing that changes.
    """

    def __init__(self, server: MCPServer) -> None:
        super().__init__(inject_idempotency_key=False, validate=False)
        self.server = server
        listed = server.handle(
            {"jsonrpc": JSONRPC_VERSION, "id": 0, "method": "tools/list"}
        )
        by_name = {s.name: s for s in server.registry.specs()}
        for described in listed["result"]["tools"]:
            spec = by_name[described["name"]]
            self.register(spec, _unreachable)

    def dispatch(
        self,
        call: ToolCall,
        run_id: str | None = None,
        step: int | None = None,
    ) -> ToolResult:
        """Send the call over the protocol and normalise the reply."""
        reply = self.server.handle(
            {
                "jsonrpc": JSONRPC_VERSION,
                "id": call.id,
                "method": "tools/call",
                "params": {
                    "id": call.id,
                    "name": call.name,
                    "arguments": dict(call.arguments),
                    "_meta": {"run_id": run_id or "local", "step": step or 0},
                },
            }
        )
        if "error" in reply:
            error = reply["error"]
            return ToolResult.failure(
                call.id,
                f"{error['message']} (mcp {error['code']})",
                # An authorization refusal is permanent. Reporting it as
                # retryable sends the agent round the loop against a wall.
                retryable=False,
            )
        result = reply["result"]
        return ToolResult(
            call_id=call.id,
            ok=not result.get("isError", False),
            content=result.get("structuredContent"),
        )


def _unreachable(**_: Any) -> Any:
    """Placeholder implementation. The gateway owns the real one.

    Raises:
        RuntimeError: Always. If this runs, something bypassed the gateway,
            and a bypassable enforcement point is not one.
    """
    raise RuntimeError("tool executed locally; the gateway was bypassed")


def registry_for(world: World) -> GatewayRegistry:
    """The registry the agent runs against: MCP, with policy in front."""
    return GatewayRegistry(MCPServer(world))


def serve_stdio(
    stdin: TextIO | None = None,
    stdout: TextIO | None = None,
    world: World | None = None,
) -> int:
    """Serve JSON-RPC over stdio: one request per line, one reply per line.

    Returns:
        Zero when the input stream ends cleanly.
    """
    source: Iterable[str] = stdin if stdin is not None else sys.stdin
    sink = stdout if stdout is not None else sys.stdout
    server = MCPServer(world if world is not None else World())
    for line in source:
        if not line.strip():
            continue
        sink.write(json.dumps(server.handle(json.loads(line))) + "\n")
        sink.flush()
    return 0


if __name__ == "__main__":
    sys.exit(serve_stdio())
