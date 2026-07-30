"""The adapter the scorecard runs against, so the demo needs no account.

A cross-cloud comparison is only meaningful when the workload, the auth
boundary, and the success definition are held identical. This adapter
exists so you can see the *mechanics* of that — the same task set, the same
approval threshold, the same graders, unmeasured fields reported as
``None`` — without an account on anything.

It is not a simulation of any platform. It measures nothing about AWS,
Google Cloud, or Azure, and :meth:`MockCloud.cold_start_ms` returns
``None`` for exactly that reason.
"""

from __future__ import annotations

from northstar_policy import Principal
from northstar_runtime import Checkpointer, MemoryCheckpointer

from adapters.base import ExitCost

__all__ = ["MockCloud"]


class MockCloud:
    """An in-process stand-in for a managed platform.

    Args:
        region: Reported on the scorecard. There is no region; saying
            ``local`` is more useful than borrowing one.
    """

    name = "mock"

    def __init__(self, region: str = "local") -> None:
        self.region = region
        self._store = MemoryCheckpointer()

    def session_store(self) -> Checkpointer:
        """An in-memory checkpointer. Survives nothing, and says so."""
        return self._store

    def tool_endpoint(self) -> str:
        """Where the tool gateway lives."""
        return "mock://gateway/mcp"

    def principal_for(self, inbound: dict) -> Principal:
        """Map an inbound request onto the three identities.

        The mock's inbound shape is the union of the three real ones, so a
        test can exercise the mapping without pretending to hold a token
        from anybody.
        """
        return Principal(
            user_id=inbound.get("user_id"),
            agent_id=str(inbound.get("agent_id", "northstar-support-agent")),
            operator_id=str(inbound.get("operator_id", "northstar-platform")),
            scopes=frozenset(inbound.get("scopes") or ()),
        )

    def exporter(self) -> str:
        """Where spans go. ``memory`` keeps them in process."""
        return "memory"

    # -------------------------------------------------- scorecard inputs

    def cold_start_ms(self) -> int | None:
        """``None``: nobody measured a cold start here, because there is not
        one. A missing measurement is information; a borrowed one is not.
        """
        return None

    def preview_dependencies(self) -> int:
        """No preview capabilities, because no capabilities."""
        return 0

    def exit_cost(self) -> ExitCost:
        """Nothing to exit from, which is the honest answer."""
        return ExitCost(
            self.name,
            travels=("everything",),
            rebuilt=(),
            preview_dependencies=0,
        )
