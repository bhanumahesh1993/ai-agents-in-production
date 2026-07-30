"""Make ``import wire`` mean *this* chapter's wire contract.

Two jobs, and only two.

**The path.** The book prints ``artifacts/ch10-a2a/``, and a hyphen is not
valid in a module name, so this directory is not a package. Putting it on
``sys.path`` is what makes ``from client.resolve import resolve_peer`` resolve
the same way under ``pytest`` as it does under ``python
artifacts/ch10-a2a/demo.py``. This conftest does that, and
forgets other chapters' modules, which a chapter conftest needs before its own
first local import. Evicting siblings *during collection* stays the root
``conftest.py``'s job: it is the one thing a per-directory conftest cannot do
for itself.

**The fixtures.** All function-scoped, and none of them imports this chapter's
modules -- they read the classes off ``request.module``, which is the test
module that asked for them. That is unusual enough to be worth the paragraph.

The root conftest evicts a chapter's modules from ``sys.modules`` while a
*sibling* chapter is collected. Which side of that eviction a given import
lands on depends on when the importing file was loaded, and a conftest and its
test module are not always loaded together: under ``pytest -q`` a chapter's
conftest loads immediately before that chapter's collection, but under
``pytest artifacts/ch10-a2a artifacts/ch11-tools`` both conftests load at
startup, before either directory is collected. So a conftest that imports
``peer.adapter`` at module scope, or inside a fixture body, can end up holding
a *different* ``AdmissionRefused`` class than the test module holds -- and then
``pytest.raises(AdmissionRefused)`` stops matching, silently, in one invocation
mode and not the other.

Reading the names off ``request.module`` removes the question: a fixture hands
back an instance of exactly the class its test imported. This is the general
form of the trap that bit Chapter 4's token-cost fixture; session scope makes
it worse but the cause is holding a reference across an eviction, and function
scope alone does not fix it.

A fixture here raises ``AttributeError`` if the requesting test module did not
import the name it needs. That is the intended behaviour: the test module is
the one place this chapter's imports happen.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

import pytest  # noqa: E402

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

#: The flagged order from the 09:14 incident: 2 x 12,000 cents, shipped,
#: flagged ``fraud_review``.
FRAUD_ORDER = "NR-2026-0042110"

#: The lamp-shade order, for the tests that need a claim below the peer's
#: evidence threshold: 8,400 cents in total, 3,250 of it the shade.
SMALL_ORDER = "NR-2026-0041827"

RUN_ID = "run-ch10-a2a"
TENANT = "northstar-us"


def _from_test(request: pytest.FixtureRequest, name: str) -> Any:
    """The named object, as the requesting test module imported it."""
    return getattr(request.module, name)


@pytest.fixture(autouse=True)
def _forget_startup_wiring(request: pytest.FixtureRequest) -> None:
    """Reset the module-level link the chapter's excerpt reads.

    A bare ``escalate_to_specialist`` uses whatever ``wiring.wire_link``
    installed last. Left alone, that would carry one test's tasks into the
    next.
    """
    reset = getattr(request.module, "reset_default_link", None)
    if reset is not None:
        reset()


@pytest.fixture
def peer(request: pytest.FixtureRequest) -> Any:
    """A fresh fraud review agent behind a fresh adapter, with its own world."""
    return _from_test(request, "A2AServer")()


@pytest.fixture
def link(request: pytest.FixtureRequest, peer: Any) -> Any:
    """A client wired to that peer, with a 200-cent budget of its own."""
    from northstar_policy import BudgetGuard

    run_budget = _from_test(request, "RunBudget")
    wire_link = _from_test(request, "wire_link")
    wired, _ = wire_link(
        budget=run_budget(BudgetGuard(max_cents=200)),
        server=peer,
        make_default=True,
    )
    return wired


@pytest.fixture
def card(request: pytest.FixtureRequest, link: Any) -> Any:
    """The peer's card, resolved through the pinned trust policy."""
    resolve_peer = _from_test(request, "resolve_peer")
    return resolve_peer(_from_test(request, "PEER_ID"), link.registry)


@pytest.fixture
def ctx(request: pytest.FixtureRequest) -> Any:
    """A run context that records where suspensions went."""
    return _from_test(request, "RunContext")(run_id=RUN_ID)


@pytest.fixture
def registry(link: Any) -> Any:
    """The pinned peer registry for this test's transport."""
    return link.registry


@pytest.fixture
def delegator(link: Any) -> Any:
    """Who the support agent is acting as."""
    return link.principal
