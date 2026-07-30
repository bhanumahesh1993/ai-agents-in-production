"""What a cloud must supply. Deliberately small.

The agent itself is untouched across all three platforms: the same
``AgentLoop``, the same tool contracts, the same policy, the same graders.
Each cloud gets an adapter and an infrastructure overlay, nothing more.

**Four methods.** If an adapter needs a fifth, that is a signal the
platform is reaching into a plane you meant to keep portable, and it goes
in the exit-cost note rather than into the interface. The interface is
narrow on purpose: a wide adapter is a migration you have already agreed to
pay for and not yet been billed.

Nothing in this package imports a cloud SDK, at import time or ever. The
three real adapters raise :class:`CloudUnavailable` from
:meth:`CloudAdapter.session_store` with the install command named, in the
same shape ``northstar_runtime.LiveModel`` uses for provider SDKs. The
methods that are pure — endpoints, principals, exporters — work offline and
are where the interesting differences live anyway.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from northstar_policy import Principal
from northstar_runtime import Checkpointer

__all__ = [
    "ADAPTER_METHODS",
    "CloudAdapter",
    "CloudUnavailable",
    "ExitCost",
    "extra_methods",
]

#: The four methods, named so a test can assert the interface has not
#: quietly grown. It is the cheapest portability check there is.
ADAPTER_METHODS: tuple[str, ...] = (
    "session_store",
    "tool_endpoint",
    "principal_for",
    "exporter",
)


class CloudUnavailable(RuntimeError):
    """A managed service was asked for and cannot be reached.

    Raised for a missing SDK, a missing credential, or a missing account,
    with the exact command or variable named. Never raised as a side effect
    of importing anything: this repository's test suite runs with no
    credentials, and an adapter that broke that would be an adapter nobody
    could read.
    """


@runtime_checkable
class CloudAdapter(Protocol):
    """What a cloud must supply. Deliberately small."""

    name: str

    def session_store(self) -> Checkpointer: ...
    def tool_endpoint(self) -> str: ...
    def principal_for(self, inbound: dict) -> Principal: ...
    def exporter(self) -> str: ...


def extra_methods(adapter: object) -> list[str]:
    """Public methods an adapter has beyond the four.

    A non-empty result is not a failure; it is a number for the exit-cost
    note. The rule is that the *loop* never calls them, so they cannot
    become load-bearing without someone noticing.
    """
    return sorted(
        name
        for name in dir(type(adapter))
        if not name.startswith("_")
        and callable(getattr(type(adapter), name, None))
        and name not in ADAPTER_METHODS
    )


class ExitCost:
    """What travels to another cloud, and what has to be rebuilt.

    Write this down while you still like the vendor. The portable half is
    larger than teams expect and the non-portable half is more expensive
    than they estimate, and you will not be given time to work it out when
    you need it.

    Args:
        cloud: Which platform.
        travels: Things that move unchanged.
        rebuilt: Things that must be re-implemented against the next one.
        preview_dependencies: Capabilities in preview that the design
            leans on. This count predicts unplanned work better than any
            feature matrix.
    """

    def __init__(
        self,
        cloud: str,
        travels: tuple[str, ...],
        rebuilt: tuple[str, ...],
        preview_dependencies: int = 0,
    ) -> None:
        self.cloud = cloud
        self.travels = travels
        self.rebuilt = rebuilt
        self.preview_dependencies = preview_dependencies

    def to_dict(self) -> dict[str, Any]:
        """JSON-serialisable form, for a decision record."""
        return {
            "cloud": self.cloud,
            "travels": list(self.travels),
            "rebuilt": list(self.rebuilt),
            "preview_dependencies": self.preview_dependencies,
        }


#: What travels, for every platform. Identical by construction, which is
#: the chapter's claim and the artifact's job to keep true.
PORTABLE: tuple[str, ...] = (
    "agent code and the loop",
    "tool contracts and their conformance tests",
    "evaluation datasets and graders",
    "OpenTelemetry spans",
    "policy intent",
)
