"""Path handling and fixtures for the Chapter 12 sandbox artifact.

Two things live here.

**The path.** This directory is not a package, because the book prints
``artifacts/ch12-sandbox/`` and a hyphen is not a valid module name. Putting the
directory on ``sys.path`` is what makes ``import egress`` resolve the same way
it does when a reader runs ``python artifacts/ch12-sandbox/demo.py``. Sibling
chapters that share a module basename are handled by the repository-root
conftest, not here.

**The fixtures.** Every egress assertion runs against a *reachable* stub rather
than against the real metadata address. Probing ``169.254.169.254`` from a
machine with no route to it passes whether or not a policy exists, which makes
it worse than no test: it reports a control you do not have. So the stub listens
on loopback, resolves under a test hostname inside the sandbox, and a pass means
the policy denied.

The ``sandbox`` fixture is parameterised over every rung the machine can build,
so adding a stronger isolation adapter later means proving it satisfies these
assertions rather than assuming a stronger rung must be safer.
"""

from __future__ import annotations

import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from ladder import build_rung, rung_names  # noqa: E402
from netshim import NetworkConfig  # noqa: E402
from sandbox import Sandbox  # noqa: E402
from stub_network import PUBLIC_HOST, StubNetwork  # noqa: E402


@pytest.fixture
def stub_service() -> Iterator[StubNetwork]:
    """A loopback HTTP service standing in for the hosts under test.

    Function-scoped on purpose: a session-scoped server outlives the rung it
    was wired to, and the routes it hands out embed a port.
    """
    net = StubNetwork()
    try:
        yield net
    finally:
        net.close()


@pytest.fixture
def stub_metadata(stub_service: StubNetwork) -> str:
    """The URL of the stub standing in for a cloud metadata endpoint."""
    return stub_service.metadata_url


@pytest.fixture
def no_egress_net(stub_service: StubNetwork) -> NetworkConfig:
    """A network config that denies everything, with offline routes."""
    return stub_service.network()


@pytest.fixture(params=rung_names(include_control=False))
def sandbox(
    request: pytest.FixtureRequest,
    no_egress_net: NetworkConfig,
) -> Iterator[Sandbox]:
    """Each available isolation rung, with egress denied.

    The negative control is excluded here: it enforces nothing by design, so
    asserting that it denies would be asserting the opposite of its purpose.
    ``test_ch12.py`` builds the full ladder itself where the control matters.
    """
    box = build_rung(request.param, no_egress_net)
    try:
        yield box
    finally:
        box.reset()


@pytest.fixture(params=rung_names(include_control=False))
def allowing_sandbox(
    request: pytest.FixtureRequest,
    stub_service: StubNetwork,
) -> Iterator[Sandbox]:
    """Each rung, with exactly one host allowed.

    Paired with ``sandbox`` so the suite proves both directions: a policy that
    denies what it should and permits what it names. A policy only ever
    observed denying could be a broken network.
    """
    net = stub_service.network(allow_hosts=frozenset({PUBLIC_HOST}))
    box = build_rung(request.param, net)
    try:
        yield box
    finally:
        box.reset()
