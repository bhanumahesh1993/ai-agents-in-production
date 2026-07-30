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


def _colliding_basenames() -> frozenset[str]:
    """Module basenames that exist in more than one artifact directory.

Kept for the record and for tooling, though eviction is deliberately broader
    than this set: restricting eviction to colliding names left a stale
    transitive reference, because a uniquely-named module (ch04's pattern table)
    can itself hold the wrong chapter's ``router``. Any module belonging to
    another artifact directory is therefore evicted.
    """
    # Only top-level modules. A package submodule is reached through its
    # package, and evicting one leaves the parent holding a stale reference --
    # which is how ch16's ``detectors.repetition`` stopped resolving. Never
    # ``__init__`` or ``conftest``, for the same reason.
    skip = {"__init__", "conftest"}
    seen: dict[str, int] = {}
    for d in ARTIFACTS.glob("ch*-*"):
        if not d.is_dir():
            continue
        names = {f.stem for f in d.glob("*.py") if f.stem not in skip}
        # Packages count too, and by directory name. Counting only ``*.py``
        # missed the sharpest case in this repository: ch15 has a
        # ``detectors.py`` module and ch16 a ``detectors/`` package, so
        # ``import detectors.repetition`` in ch16 resolved to ch15's module and
        # failed with "detectors is not a package".
        names |= {sub.parent.name for sub in d.glob("*/__init__.py")}
        for name in names:
            seen[name] = seen.get(name, 0) + 1
    return frozenset(name for name, n in seen.items() if n > 1)


_COLLIDING = _colliding_basenames()


def _evict_siblings(here: Path) -> None:
    """Forget *colliding* modules imported from a different artifact directory.

    Each chapter's own ``conftest.py`` already puts its directory on the path,
    which suffices when that chapter runs alone. It does not suffice in one run
    over all of ``artifacts/``: once ``router`` is in ``sys.modules`` from ch04,
    ch25's ``import router`` is served from the cache and never consults the
    path. Eviction is the part a per-directory conftest cannot do for itself.
    """
    here = here.resolve()

    # Put this chapter first. Each chapter's conftest inserts its own directory,
    # but those insertions accumulate across a full run, so by the time ch16's
    # tests execute another chapter can precede it and ``import detectors``
    # resolves to a namespace package found earlier on the path.
    if str(here) in sys.path:
        sys.path.remove(str(here))
    sys.path.insert(0, str(here))

    others = [
        str(d.resolve())
        for d in ARTIFACTS.glob("ch*-*")
        if d.is_dir() and d.resolve() != here
    ]
    for name, mod in list(sys.modules.items()):
        if name in ("conftest",) or name.endswith(".conftest"):
            continue
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
