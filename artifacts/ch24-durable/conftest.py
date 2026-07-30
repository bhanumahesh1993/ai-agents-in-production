"""Make ``import workflow`` mean *this* chapter's workflow.

The book prints ``artifacts/ch24-durable/`` and a hyphen is not a legal
module name, so this directory is not a package. Its modules are imported by
plain name, and several of those names -- ``workflow``, ``stream``,
``crash``, ``demo`` -- are used by other chapters too. Putting this
directory on ``sys.path`` is what makes ``python
artifacts/ch24-durable/demo.py`` and this chapter's tests resolve their
imports the same way.

Evicting whatever a *sibling* chapter left in ``sys.modules`` is the other
half of that, and it is not done here: the repository-root ``conftest.py``
owns it, because it fires before a module is imported and this file does
not. All this file owns is the path and the fixtures below.

The fixtures are function-scoped on purpose. A session-scoped fixture that
captures a module-level object binds whichever chapter's module happened to
be in the cache when the fixture first ran, which is a failure that only
appears under a full-repository run.
"""

from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent

sys.path.insert(0, str(HERE))

import pytest  # noqa: E402

from northstar_contracts import World  # noqa: E402


@pytest.fixture
def world() -> World:
    """A fresh Northstar world, so one test's refunds cannot reach another."""
    return World()


@pytest.fixture
def journal_dir() -> Path:
    """The shipped replay corpus."""
    return HERE / "journals"
