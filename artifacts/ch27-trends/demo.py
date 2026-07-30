"""Validate the repository's dated claims file, and prove the check bites.

    python artifacts/ch27-trends/demo.py
    python artifacts/ch27-trends/demo.py --as-of "Jan 2027"

The Chapter 27 artifact points at ``trend-tracker.md`` in the repository
root. Every forward-looking or volatile claim in the chapter lives there as
a row with a kind, a source organisation, a source date, a last-verified
date, and the chapters that rely on it, so a future edition can be
re-verified without rewriting a paragraph of prose.

This demo does three things:

1. **Validates the real file.** Every row categorised, attributed, dated,
   and traceable; no claim verified before it was made; nothing left
   unverified except the rows the chapter deliberately declined to assert.
2. **Ages it.** Staleness against the per-kind cadence derived from the
   chapter's rot-watch table. Reported, never fatal — a check that starts
   failing because a date rolled over is a check people delete.
3. **Falsifies itself.** Runs the same validator over a file with one of
   each defect planted in it, and confirms every one is caught. A
   validator nobody has watched fail is a validator that might be
   returning green for the wrong reason.

Exits non-zero if the real file has a structural problem, if the planted
defects are not all caught, or if `--strict-age` is given and a row is
overdue.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import argparse  # noqa: E402
import tempfile  # noqa: E402
from datetime import date  # noqa: E402

from tracker import (  # noqa: E402
    KINDS,
    MAX_AGE_MONTHS,
    Tracker,
    default_tracker_path,
    parse_tracker,
    staleness,
    validate,
)

#: One planted defect per row, assembled rather than written out, so the
#: fixture stays inside the repository's own column limit.
_BROKEN_CLAIMS = (
    # claim, kind, source org, source date, verified, book ref
    ("Agents will be very good at everything quite soon",
     "vibes", "METR", "Mar 2025", "Jul 2026", "Ch 27"),
    ("Time horizons have been doubling every seven months",
     "measurement", "-", "Mar 2025", "Jul 2026", "Ch 27"),
    ("Some protocol shipped a revision at some point",
     "status", "Linux Foundation", "sometime last year", "Jul 2026",
     "Ch 27"),
    ("MCP is stewarded by a neutral foundation",
     "status", "Linux Foundation", "Dec 2025", "**unverified**", "Ch 27"),
    ("Enterprise agent identity products reached availability",
     "status", "vendor docs", "Jan 2026", "Mar 2025", "Ch 27"),
    ("Agent commerce protocols launched with many backers",
     "directional", "vendor announcements", "Sep 2025", "Jul 2026",
     "chapter twenty-seven"),
    ("Agents", "forecast", "METR", "Mar 2025", "Jul 2026", "Ch 27"),
)

_BROKEN_ROT = (
    ("Cloud pricing", "glacial", "monthly"),
    ("Framework versions", "high", ""),
)


def _table(header: tuple[str, ...], rows: tuple[tuple[str, ...], ...]) -> str:
    """Render one Markdown table."""
    lines = [
        "| " + " | ".join(header) + " |",
        "|" + "|".join("---" for _ in header) + "|",
    ]
    lines.extend("| " + " | ".join(row) + " |" for row in rows)
    return "\n".join(lines)


BROKEN = "\n\n".join(
    [
        "# a deliberately broken claims file",
        _table(
            ("#", "Claim", "kind", "Source org", "Source date", "verified",
             "Book ref"),
            tuple(
                (str(index), *row)
                for index, row in enumerate(_BROKEN_CLAIMS, start=1)
            ),
        ),
        "## Rot rates",
        _table(("Layer", "Rot rate", "Re-verify"), _BROKEN_ROT),
        "",
    ]
)

EXPECTED_DEFECTS = {
    "kind",
    "source org",
    "source date",
    "verified",
    "book ref",
    "claim",
    "rot rate",
    "re-verify",
}


def month_today() -> str:
    """Today, in the form the tracker writes dates."""
    return date.today().strftime("%b %Y")


def report_file(tracker: Tracker) -> None:
    """Print what the file contains, by category."""
    print(f"file            : {tracker.path}")
    print(f"claims          : {len(tracker.claims)}")
    counts = tracker.by_kind()
    for kind in KINDS:
        print(f"  {kind:<14}{counts[kind]:>3}   (stale after "
              f"{MAX_AGE_MONTHS[kind]} months)")
    covered = tracker.chapters_covered()
    print(f"chapters relying on a dated claim: {covered}")
    print(f"rot-watch layers: {len(tracker.rot_rows)}")

    unverified = [c for c in tracker.claims if c.is_unverified]
    print(f"\nrows the book deliberately does not assert: {len(unverified)}")
    for claim in unverified:
        print(f"  {claim.number}. [{claim.kind}] {claim.text[:58]}...")
        print(f"     source: {claim.source_org}, {claim.source_date}")


def report_staleness(tracker: Tracker, as_of: str) -> list[str]:
    """Print the ageing report and return the overdue rows."""
    overdue = staleness(tracker, as_of)
    print(f"\n=== ageing, as of {as_of} ===")
    if not overdue:
        print("  every verified row is inside its re-verification cadence")
        return []
    for claim, age, allowance in overdue:
        print(
            f"  row {claim.number:>2} [{claim.kind:<11}] {age} months old, "
            f"allowance {allowance}: {claim.text[:40]}..."
        )
    return [c.number for c, _, _ in overdue]


def prove_the_checker_bites() -> tuple[set[str], list[str]]:
    """Validate a file with one of each defect planted in it."""
    with tempfile.TemporaryDirectory(prefix="ch27-") as tmp:
        path = Path(tmp) / "broken-tracker.md"
        path.write_text(BROKEN, encoding="utf-8")
        problems = validate(parse_tracker(path))
    return {p.field for p in problems}, [p.line() for p in problems]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--as-of",
        default=month_today(),
        help='date to age the file against, e.g. "Jan 2027"',
    )
    parser.add_argument(
        "--strict-age",
        action="store_true",
        help="fail when a row is past its re-verification cadence",
    )
    parser.add_argument(
        "--file",
        default=str(default_tracker_path()),
        help="tracker to validate; defaults to the repository root file",
    )
    args = parser.parse_args()

    failures: list[str] = []

    print("=== the claims file ===")
    tracker = parse_tracker(Path(args.file))
    report_file(tracker)

    print("\n=== structural validation ===")
    problems = validate(tracker)
    if problems:
        for problem in problems:
            print(f"  {problem.line()}")
        failures.append(f"{len(problems)} structural problem(s) in the file")
    else:
        print("  every claim has a kind, a source org, a source date, a")
        print("  verified date, and a book reference; no claim is verified")
        print("  before it was made; only directional rows are unverified")

    overdue = report_staleness(tracker, args.as_of)
    if overdue and args.strict_age:
        failures.append(f"{len(overdue)} row(s) past their cadence")

    print("\n=== the checker, falsified ===")
    fields, lines = prove_the_checker_bites()
    for line in lines:
        print(f"  caught {line}")
    missed = EXPECTED_DEFECTS - fields
    if missed:
        failures.append(f"the validator missed: {', '.join(sorted(missed))}")
    else:
        print(f"\n  all {len(EXPECTED_DEFECTS)} planted defects were caught")

    print("\n--- what this proves ---")
    print("A book about a fast-moving field stays maintainable by keeping")
    print("volatile claims out of the prose. Every dated assertion in")
    print("Chapter 27 is one row with its category, its source, and the")
    print("date it was last checked, so a reader can tell what to distrust")
    print("without re-deriving it, and a future edition can refresh the")
    print("file rather than the chapter.")

    if failures:
        print("\nFAILED:")
        for line in failures:
            print(f"  - {line}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
