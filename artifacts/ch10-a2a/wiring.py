"""The assembly point: the one module allowed to know both halves.

``client/`` must not import ``peer/`` and ``peer/`` must not import
``client/``, or the artifact is demonstrating an in-process call with extra
steps. But something has to put the two together, and in a real deployment
that something is startup configuration on each side plus a url. Here it is
this file.

Keeping it separate buys two things. Architecturally, every line in
``client/`` stays honest: nothing there can reach the peer's code even by
accident, and a reader can confirm that by grepping the imports. Practically,
it removes the last lazy import from the chapter -- ``wire_link`` used to
import ``peer.adapter`` inside its own body, which works fine until the test
suite's module isolation evicts that module between two chapters' collection
and the next call re-imports it, giving you two ``AdmissionRefused`` classes
and an exception handler that silently stops matching. Import-time wiring in
one module cannot do that.
"""

from __future__ import annotations

from typing import Any

from client.escalate import (
    Delegator,
    PeerLink,
    RunBudget,
    default_principal,
    set_default_link,
)
from client.resolve import PeerRegistry
from peer.adapter import A2AServer
from transport import MockTransport

__all__ = ["wire_link"]


def wire_link(
    *,
    principal: Delegator | None = None,
    budget: RunBudget | None = None,
    server: A2AServer | None = None,
    make_default: bool = False,
) -> tuple[PeerLink, A2AServer]:
    """Stand up a client and a peer with no socket between them.

    Args:
        principal: Who the client acts as.
        budget: The run's remaining spend.
        server: An already-built peer to mount. A fresh one by default, which
            is what stops two links sharing a task store.
        make_default: Install the result as the link a bare
            :func:`client.escalate.escalate_to_specialist` will use, the way a
            service's startup does once. Off by default: a demo or a test that
            builds a throwaway peer to watch it refuse something must not
            silently repoint the link the rest of the run is using.

    Returns:
        ``(link, server)``. The server is returned so a demo or a test can
        assert on what the peer actually did -- how many reviews it opened,
        what its audit log says -- rather than on what the client believes.
    """
    server = server or A2AServer()
    card: dict[str, Any]
    card, _ = server.agent_card()
    url = str(card["supportedInterfaces"][0]["url"])
    transport = MockTransport().mount(url, server)
    link = PeerLink(
        transport=transport,
        registry=PeerRegistry(transport),
        principal=principal or default_principal(),
        budget=budget or RunBudget(),
    )
    if make_default:
        set_default_link(link)
    return link, server
