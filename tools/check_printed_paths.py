"""Prove that every artifact path printed in the book exists in this repo.

An external audit found the book printing 28 `artifacts/chNN-slug/` paths while
the repository contained one directory, under a different name. That class of
defect is invisible to a test suite -- every test passed -- so it needs its own
check, run against the manuscript rather than against the code.

    python3 tools/check_printed_paths.py                    # uses ../manuscript
    python3 tools/check_printed_paths.py path/to/manuscript

Exits non-zero if any printed path is missing, or if any artifact directory is
missing the files this repository promises for every chapter.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
DEFAULT_MANUSCRIPT = REPO.parent / "manuscript"

# Every chapter directory must carry these, because the book's "In the repo"
# sections promise a runnable demo, a test, and an explanation.
REQUIRED = ("README.md", "demo.py")

PATH_RE = re.compile(r"artifacts/(ch\d\d[a-z0-9\-]*)/?")


def printed_paths(manuscript: Path) -> dict[str, set[str]]:
    """Map each printed artifact directory to the files that cite it."""
    found: dict[str, set[str]] = {}
    for md in sorted(manuscript.glob("*.md")):
        for name in PATH_RE.findall(md.read_text()):
            found.setdefault(name, set()).add(md.name)
    return found


def main() -> int:
    manuscript = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_MANUSCRIPT
    if not manuscript.is_dir():
        print(f"manuscript directory not found: {manuscript}")
        return 2

    cited = printed_paths(manuscript)
    if not cited:
        print(f"no artifact paths found in {manuscript}; check the path")
        return 2

    problems: list[str] = []
    for name in sorted(cited):
        d = REPO / "artifacts" / name
        if not d.is_dir():
            where = ", ".join(sorted(cited[name]))
            problems.append(f"MISSING  artifacts/{name}/  (printed in {where})")
            continue
        for f in REQUIRED:
            if not (d / f).exists():
                problems.append(f"INCOMPLETE  artifacts/{name}/{f}")
        if not list(d.glob("test_*.py")):
            problems.append(f"INCOMPLETE  artifacts/{name}/  has no test_*.py")

    # A directory that exists but is never printed is also a defect: it means
    # the book and the repo have drifted apart in the other direction.
    on_disk = {d.name for d in (REPO / "artifacts").glob("ch*") if d.is_dir()}
    for extra in sorted(on_disk - set(cited)):
        problems.append(f"UNREFERENCED  artifacts/{extra}/ is not printed")

    print(f"printed artifact paths: {len(cited)}")
    print(f"directories on disk:    {len(on_disk)}")
    if problems:
        print(f"\nFAIL: {len(problems)} problem(s)\n")
        for p in problems:
            print("  -", p)
        return 1
    print("\nOK: every printed path exists and carries a README, demo, and test")
    return 0


if __name__ == "__main__":
    sys.exit(main())
