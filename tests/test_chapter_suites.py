"""Run each chapter's suite in its own process.

The chapters legitimately share module basenames that the book prints:
``router.py`` in ch04 and ch25, ``budget.py`` in three chapters, ``demo.py`` in
all of them, and -- the sharpest case -- ch15's ``detectors.py`` module against
ch16's ``detectors/`` package. Collecting all of them into one interpreter makes
``import router`` mean whichever chapter was imported first.

Manipulating ``sys.path`` and the import cache to compensate was tried and
abandoned. It can be made to pass, but not *stably*: a chapter's test module is
imported once at collection and keeps its globals, so evicting that chapter's
modules while another chapter runs leaves those globals pointing at objects that
no longer match what a fixture rebuilds. Chapter 4's token-cost comparison
failed on one run and passed on the next for exactly that reason.

A process per chapter removes the class of problem instead of managing it, and it
matches how a reader actually runs one chapter. ``pytest -q`` at the repository
root therefore covers the shared packages directly and every chapter through
this file.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
ARTIFACTS = REPO / "artifacts"

CHAPTERS = sorted(
    d.name for d in ARTIFACTS.glob("ch*-*") if d.is_dir() and any(
        d.glob("test_*.py")
    ) or (d / "tests").is_dir()
)


def test_every_printed_chapter_has_a_suite() -> None:
    """A chapter directory with no tests would silently pass this file."""
    dirs = sorted(d.name for d in ARTIFACTS.glob("ch*-*") if d.is_dir())
    missing = [d for d in dirs if d not in CHAPTERS]
    assert not missing, f"chapter directories with no tests: {missing}"
    assert len(CHAPTERS) == 28, f"expected 28 chapters, found {len(CHAPTERS)}"


@pytest.mark.parametrize("chapter", CHAPTERS)
def test_chapter_suite_passes_in_isolation(chapter: str) -> None:
    """Run one chapter's tests in a fresh interpreter."""
    proc = subprocess.run(
        [
            sys.executable, "-m", "pytest", "-q",
            "-p", "no:cacheprovider",
            f"artifacts/{chapter}",
        ],
        cwd=REPO,
        capture_output=True,
        text=True,
        timeout=600,
    )
    if proc.returncode != 0:
        tail = "\n".join((proc.stdout + proc.stderr).splitlines()[-25:])
        pytest.fail(f"artifacts/{chapter} failed:\n{tail}")
