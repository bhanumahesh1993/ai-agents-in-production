"""Make ``import policy`` mean *this* chapter's policy.

The book prints ``artifacts/ch18-redteam/`` and a hyphen is not a legal module
name, so this directory is not a package and its modules are imported by plain
name. ``policy.py``, ``cases.py``, and ``demo.py`` all exist in other chapters,
so putting this directory on ``sys.path`` is what makes ``python
artifacts/ch18-redteam/demo.py`` and these tests resolve the same modules.

Evicting whatever a *sibling* chapter left in ``sys.modules`` is the other half
of that, and it is not done here: the repository-root ``conftest.py`` owns it,
because it fires before a module is imported and this file does not.

Every fixture is function-scoped, and each one runs its own case. A shared
result object would be a shared ``World``, and one test's planted canary
reaching another test is the exact confusion this chapter is about.
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
    """A fresh Northstar world, with nothing planted in it yet."""
    return World()


@pytest.fixture
def fixture_dir() -> Path:
    """The directory holding the poisoned supplier page."""
    return HERE / "fixtures"


@pytest.fixture(params=["inj-001", "inj-002"])
def case(request: pytest.FixtureRequest):  # noqa: ANN201 - type is in cases.py
    """Each injection case in turn, so every test covers both vectors."""
    import cases

    return cases.case_by_id(str(request.param))
