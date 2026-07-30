"""Print the artifact manifest: chapter -> files, tests, and demo.

The audit asked for a release candidate to "emit an artifact manifest that maps
each chapter to its files and tests". This is that, and it doubles as a quick
way to see which chapters are still thin.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def main() -> int:
    rows = []
    for d in sorted((REPO / "artifacts").glob("ch*-*")):
        if not d.is_dir():
            continue
        files = sorted(f.name for f in d.glob("*.py"))
        tests = [f for f in files if f.startswith("test_")]
        extras = sorted(
            f.name for f in d.iterdir()
            if f.is_file() and f.suffix in {".yaml", ".yml", ".json", ".tf",
                                            ".md", ".sh", ".toml"}
        )
        rows.append((d.name, len(files), len(tests),
                     (d / "demo.py").exists(), len(extras)))

    if not rows:
        print("no artifact directories found")
        return 1

    w = max(len(r[0]) for r in rows)
    print(f"{'chapter':<{w}}  py  tests  demo  other")
    print("-" * (w + 24))
    incomplete = 0
    for name, n_py, n_test, has_demo, n_extra in rows:
        flag = "" if (has_demo and n_test) else "   <- incomplete"
        if flag:
            incomplete += 1
        print(f"{name:<{w}}  {n_py:>2}  {n_test:>5}  "
              f"{'yes' if has_demo else 'NO ':>4}  {n_extra:>5}{flag}")
    print(f"\n{len(rows)} chapter directories, {incomplete} incomplete")
    return 1 if incomplete else 0


if __name__ == "__main__":
    sys.exit(main())
