"""Make ``import pause`` mean *this* chapter's pause.

The book prints ``artifacts/ch08-long-horizon/`` and a hyphen is not a legal
module name, so this directory is not a package and its modules are imported
by plain name. Putting the directory on ``sys.path`` is what makes ``python
artifacts/ch08-long-horizon/demo.py`` and this chapter's tests resolve
``pause``, ``resume``, ``keys``, and the rest the same way.

Evicting whatever a *sibling* chapter left in ``sys.modules`` is the other
half of that, and it is not done here: the repository-root ``conftest.py``
owns it, because it fires before a module is imported and this file does not.

Both fixtures are function-scoped, and the state directory is per test. This
chapter's stores are files, and a file shared between tests is a test that
passes in isolation and fails in a suite -- which is the same class of bug the
chapter is about.
"""

from __future__ import annotations

import sys
from collections.abc import Iterator
from pathlib import Path

HERE = Path(__file__).resolve().parent

sys.path.insert(0, str(HERE))

import pytest  # noqa: E402
import wiring  # noqa: E402


@pytest.fixture
def state_dir(tmp_path: Path) -> Path:
    """A private directory for this test's ``run.db`` and ``refunds.db``."""
    directory = tmp_path / "state"
    directory.mkdir()
    return directory


@pytest.fixture
def opened(state_dir: Path) -> Iterator[list[wiring.Wiring]]:
    """A registry of wirings this test opened, closed on the way out.

    Each entry stands for one worker process. Tests append to it through
    :func:`open_worker`, and SQLite handles are released even when an
    assertion fails part-way through a phase.
    """
    wirings: list[wiring.Wiring] = []
    yield wirings
    for wired in reversed(wirings):
        wired.close()
