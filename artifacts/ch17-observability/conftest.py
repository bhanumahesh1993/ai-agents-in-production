"""Make ``import spans`` mean *this* chapter's spans.

The book prints ``artifacts/ch17-observability/`` and a hyphen is not a legal
module name, so this directory is not a package and its modules are imported by
plain name. ``cost.py`` and ``demo.py`` exist in other chapters too, so putting
this directory on ``sys.path`` is what makes ``python
artifacts/ch17-observability/demo.py`` and these tests resolve the same
modules.

Evicting whatever a *sibling* chapter left in ``sys.modules`` is the other half
of that, and it is not done here: the repository-root ``conftest.py`` owns it,
because it fires before a module is imported and this file does not.

Both fixtures below are function-scoped and each builds its own suite. That is
slower than sharing one and it is the right trade: a session-scoped fixture
that captured a module-level object would bind whichever chapter's ``cost``
module happened to be cached when it first ran.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Iterator
from pathlib import Path

HERE = Path(__file__).resolve().parent

sys.path.insert(0, str(HERE))

import pytest  # noqa: E402


@pytest.fixture
def unset_exporter() -> Iterator[None]:
    """Run with no ``NORTHSTAR_OTEL_EXPORTER`` set, whatever the shell has.

    The default matters -- it is the difference between a chapter that runs
    offline and one that needs a collector -- so the test that asserts it
    cannot inherit a developer's environment.
    """
    previous = os.environ.pop("NORTHSTAR_OTEL_EXPORTER", None)
    try:
        yield
    finally:
        if previous is not None:
            os.environ["NORTHSTAR_OTEL_EXPORTER"] = previous


@pytest.fixture
def propagated():  # noqa: ANN201 - the return type lives in tickets.py
    """The suite with the escalation edge intact."""
    import tickets

    return tickets.run_suite(propagate=True, exporter="memory")


@pytest.fixture
def broken():  # noqa: ANN201 - the return type lives in tickets.py
    """The same suite with context propagation broken: April, reproduced."""
    import tickets

    return tickets.run_suite(propagate=False, exporter="memory")
