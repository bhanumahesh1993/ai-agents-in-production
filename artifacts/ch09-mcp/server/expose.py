"""Derive the MCP tool surface from the registry, and refuse write tools.

The tool surface is derived, never hand-written. One ``ToolRegistry`` is the
source of truth and each transport renders it. Keeping a second copy of the
schemas in an MCP descriptor file is how a server's advertised contract
drifts away from what it actually does, and the drift is invisible: both
halves keep working, they just stop describing each other.

The ``raise`` is the interesting line. ``ToolSpec`` already carries a
``writes`` flag, so the read server refuses to start when a write tool is
registered on it. It is then not possible to issue a refund through this
server by any argument, prompt, or description, because the tool is not
there. Read and write tools on separate servers, under separate scopes, is
the cheapest structural control in Chapter 9, and it costs one conditional.
"""

from __future__ import annotations

from northstar_contracts import World
from northstar_runtime import ToolRegistry

__all__ = ["ConfigError", "descriptors", "read_only_registry"]


class ConfigError(Exception):
    """A server was configured with a tool that does not belong on it.

    Raised at construction, not at call time. A misconfigured server that
    starts and then refuses one call in fifty is a server that ships.
    """


def descriptors(registry: ToolRegistry) -> list[dict]:
    """Expose read tools only. A write tool is a build error.

    Args:
        registry: The single source of truth for this server's tools.

    Returns:
        One MCP descriptor per registered tool, in registration order.

    Raises:
        ConfigError: If any registered spec declares ``writes=True``.
    """
    out: list[dict] = []
    for spec in registry.specs():
        if spec.writes:
            raise ConfigError(f"{spec.name} writes; wrong server")
        out.append({
            "name": spec.name,
            "description": spec.description,
            "inputSchema": spec.input_schema,
            "outputSchema": spec.output_schema,
        })
    return out


def read_only_registry(world: World) -> ToolRegistry:
    """Build the registry the read server is allowed to hold.

    The filter is ``spec.writes``, read off the contract, rather than a
    hand-kept list of three names that someone will extend to four.

    Args:
        world: The Northstar world holding the six tool implementations.

    Returns:
        A registry carrying ``get_order``, ``get_policy``, ``search_orders``.
    """
    registry = ToolRegistry(validate=True)
    registry.register_all(
        (spec, fn) for spec, fn in world.tools() if not spec.writes
    )
    return registry
