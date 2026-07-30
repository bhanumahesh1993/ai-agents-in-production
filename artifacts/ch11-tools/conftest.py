"""Make ``import budget`` mean *this* chapter's budget module.

Two jobs, and only two.

**The path.** The book prints ``artifacts/ch11-tools/``, and a hyphen is not
valid in a module name, so this directory is not a package. Putting it on
``sys.path`` is what makes ``from specs import ISSUE_REFUND`` resolve the same
way under ``pytest`` as it does under ``python artifacts/ch11-tools/demo.py``.
It matters more here than in most chapters: ``budget.py`` exists in three other
chapters and ``demo.py`` in every one. This conftest does
the path work and forgets other chapters' modules, which a chapter conftest
needs before its own first local import. Evicting siblings *during collection*
stays the root ``conftest.py``'s job.

**The fixtures.** All function-scoped, and none of them imports this chapter's
modules -- they read the classes off ``request.module``, which is the test
module that asked for them. That is unusual enough to be worth the paragraph.

The root conftest evicts a chapter's modules from ``sys.modules`` while a
*sibling* chapter is collected. Which side of that eviction a given import
lands on depends on when the importing file was loaded, and a conftest and its
test module are not always loaded together: under ``pytest -q`` this conftest
loads immediately before this directory's collection, but under ``pytest
artifacts/ch10-a2a artifacts/ch11-tools`` both chapters' conftests load at
startup, before either directory is collected. A conftest that imports
``sandbox`` at module scope, or inside a fixture body, can then hold a
*different* ``SandboxDenied`` class than the test module holds -- and
``pytest.raises(SandboxDenied)`` stops matching, silently, in one invocation
mode and not the other.

Reading the names off ``request.module`` removes the question: a fixture hands
back an instance of exactly the class its test imported. This is the general
form of the trap that bit Chapter 4's token-cost fixture; session scope makes
it worse, but the cause is holding a reference across an eviction and function
scope alone does not fix it.

Every fixture also builds a fresh world, ledger, and registry, so no test can
see another test's refunds -- which for a library whose central property is
"exactly one refund row" is the difference between a suite that means something
and one that does not.

A fixture here raises ``AttributeError`` if the requesting test module did not
import the name it needs. That is intended: the test module is the one place
this chapter's imports happen.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

import pytest  # noqa: E402

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

#: The order from the opening incident: 8,400 cents in total, of which the
#: lamp shade is 3,250.
ORDER = "NR-2026-0041827"
ORDER_TOTAL_CENTS = 8400
LAMP_SHADE_CENTS = 3250

#: The flagged order, for the escalation path.
FRAUD_ORDER = "NR-2026-0042110"

#: Refunds at or above this need a human.
APPROVAL_THRESHOLD_CENTS = 5000

RUN_ID = "run-ch11-tools"


def _from_test(request: pytest.FixtureRequest, name: str) -> Any:
    """The named object, as the requesting test module imported it."""
    return getattr(request.module, name)


@pytest.fixture
def world(request: pytest.FixtureRequest) -> Any:
    """A fresh system of record. Three orders, no refunds, no faults."""
    return _from_test(request, "World")()


@pytest.fixture
def ledger(request: pytest.FixtureRequest) -> Any:
    """An empty side-effect ledger."""
    return _from_test(request, "SideEffectLedger")()


@pytest.fixture
def sandbox(request: pytest.FixtureRequest) -> Any:
    """A sandbox with the default contract: deny egress, no credentials."""
    null_sandbox = _from_test(request, "NullSandbox")
    contract = _from_test(request, "SandboxContract")
    return null_sandbox(contract())


@pytest.fixture
def path(request: pytest.FixtureRequest, world: Any, ledger: Any) -> Any:
    """The refund path, bound to this test's world and ledger."""
    return _from_test(request, "RefundPath")(world=world, ledger=ledger)


@pytest.fixture
def library(
    request: pytest.FixtureRequest,
    world: Any,
    ledger: Any,
    sandbox: Any,
) -> Any:
    """The whole eight-tool library, registered and conformant."""
    return _from_test(request, "build_library")(world, ledger, sandbox)
