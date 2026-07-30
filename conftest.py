"""Root pytest configuration: import isolation for the artifact directories.

The book prints paths like ``artifacts/ch04-patterns/``. A hyphen is not valid
in a module name, so these directories are not packages, and the obvious fix --
putting all of them on ``sys.path`` -- is wrong. Eleven module basenames are
shared across chapters: ``router.py`` exists in ch04 and ch25, ``budget.py`` in
three chapters, ``demo.py`` in every one. With all directories on the path,
``import router`` resolves to whichever chapter was collected first, so a suite
that passes one directory at a time fails as a whole.

Renaming is not available: the chapters print those filenames, and the
repository's promise is that the printed path is the real path.

Each chapter's own ``conftest.py`` handles the path, because it knows its own
layout and often holds that chapter's fixtures. What it cannot do is undo the
module cache: once ``router`` is imported from ch04, ch25's ``import router``
is served from ``sys.modules`` without consulting the path. So this file does
exactly one thing, on ``pytest_collectstart``, which fires before a module is
imported: evict modules that came from a *different* artifact directory.

Running a demo directly is unaffected: ``python artifacts/chNN-slug/demo.py``
puts the script's own directory on ``sys.path`` before anything else.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).parent
ARTIFACTS = ROOT / "artifacts"

sys.path.insert(0, str(ROOT))


def _artifact_dir_of(path: Path) -> Path | None:
    """The ``artifacts/chNN-slug`` ancestor of a path, if it has one."""
    try:
        rel = path.resolve().relative_to(ARTIFACTS.resolve())
    except (ValueError, OSError):
        return None
    return (ARTIFACTS / rel.parts[0]) if rel.parts else None


def _evict_siblings(here: Path) -> None:
    """Forget modules imported from a *different* artifact directory.

    Each chapter's own ``conftest.py`` already puts its directory on the path,
    which is enough when that chapter runs alone. It is not enough in a single
    run over all of ``artifacts/``: once ``router`` is in ``sys.modules`` from
    ch04, ch25's ``import router`` is satisfied from the cache and never looks
    at the path at all. Eviction is the part a per-directory conftest cannot do
    for itself, so it is the only thing done here -- the path is left to those
    conftests, which know their own chapter's layout.
    """
    here = here.resolve()
    others = [
        str(d.resolve())
        for d in ARTIFACTS.glob("ch*-*")
        if d.is_dir() and d.resolve() != here
    ]
    for name, mod in list(sys.modules.items()):
        origin = getattr(mod, "__file__", None)
        if not origin:
            continue
        try:
            parent = str(Path(origin).resolve().parent)
        except OSError:
            continue
        if any(parent == o or parent.startswith(o + "/") for o in others):
            del sys.modules[name]


def _evict_for(path_like) -> None:
    if path_like is None:
        return
    d = _artifact_dir_of(Path(str(path_like)))
    if d is not None and d.is_dir():
        _evict_siblings(d)


def pytest_collectstart(collector) -> None:
    """Evict siblings before a chapter's modules are imported."""
    _evict_for(getattr(collector, "path", None))


def pytest_runtest_setup(item) -> None:
    """Evict again before each test runs.

    Collection-time eviction alone is not sufficient: a fixture body executes
    during test setup, long after collection, and by then another chapter's
    identically-named module may have taken the cache entry. Chapter 4's
    token-cost fixture failed exactly this way when run alongside chapters 3
    and 25, while passing on its own.
    """
    _evict_for(getattr(item, "path", None))
