"""The Chapter 27 claims-file properties, as assertions.

The file under test is ``trend-tracker.md`` at the repository root. These
tests assert on its parsed structure and on what the validator does with
planted defects, not on its wording.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import pytest  # noqa: E402
from demo import BROKEN, EXPECTED_DEFECTS  # noqa: E402
from tracker import (  # noqa: E402
    KINDS,
    MAX_AGE_MONTHS,
    Period,
    default_tracker_path,
    parse_period,
    parse_tracker,
    staleness,
    validate,
)

HEADER = (
    "| # | Claim | kind | Source org | Source date | verified | Book ref |\n"
    "|---|---|---|---|---|---|---|\n"
)
ROT = (
    "\n| Layer | Rot rate | Re-verify |\n|---|---|---|\n"
    "| Agent loop mechanics | very low | per edition |\n"
)


def _tracker(rows: str, tmp_path: Path):  # noqa: ANN202
    """Parse a synthetic claims file with the given rows."""
    path = tmp_path / "tracker.md"
    path.write_text(HEADER + rows + ROT, encoding="utf-8")
    return parse_tracker(path)


def test_the_repository_claims_file_is_structurally_valid() -> None:
    """The property the demo blocks on: the real file holds every rule."""
    tracker = parse_tracker()

    assert tracker.path == default_tracker_path()
    assert validate(tracker) == []
    assert len(tracker.claims) >= 10
    assert tracker.rot_rows, "the claims have no re-verification cadence"


def test_every_claim_carries_all_five_fields() -> None:
    """Kind, source org, source date, verified date, book ref."""
    for claim in parse_tracker().claims:
        assert claim.kind in KINDS
        assert claim.source_org
        assert parse_period(claim.source_date)
        assert claim.verified
        assert claim.chapters(), f"row {claim.number} names no chapter"
        if claim.is_unverified:
            assert claim.kind == "directional"


def test_counts_are_computed_from_the_rows() -> None:
    """The summary is a count, not a constant."""
    tracker = parse_tracker()
    counts = tracker.by_kind()

    assert sum(counts.values()) == len(tracker.claims)
    for kind in KINDS:
        assert counts[kind] == sum(
            1 for c in tracker.claims if c.kind == kind
        )
    assert 27 in tracker.chapters_covered()


def test_the_validator_catches_every_planted_defect(tmp_path: Path) -> None:
    """A checker nobody has watched fail may be green for the wrong reason."""
    path = tmp_path / "broken.md"
    path.write_text(BROKEN, encoding="utf-8")
    problems = validate(parse_tracker(path))

    assert {p.field for p in problems} >= EXPECTED_DEFECTS
    assert len(problems) >= len(EXPECTED_DEFECTS)


def test_a_status_row_may_not_be_left_unverified(tmp_path: Path) -> None:
    """A forecast is allowed to be wrong. A status fact is an erratum."""
    unverifiable = (
        "| 1 | Some protocol is stewarded by a neutral foundation | KIND "
        "| Linux Foundation | Dec 2025 | **unverified** | Ch 27 |\n"
    )
    status = validate(_tracker(unverifiable.replace("KIND", "status"), tmp_path))
    directional = validate(
        _tracker(unverifiable.replace("KIND", "directional"), tmp_path)
    )

    assert [p.field for p in status] == ["verified"]
    assert directional == []


def test_a_claim_cannot_be_verified_before_it_was_made(tmp_path: Path) -> None:
    """The cheapest inconsistency to introduce by hand, and to catch."""
    backwards = (
        "| 1 | A product reached general availability in this window "
        "| status | vendor docs | Jan 2026 | Mar 2025 | Ch 19 |\n"
    )
    problems = validate(_tracker(backwards, tmp_path))

    assert len(problems) == 1
    assert "predates" in problems[0].message


def test_row_numbering_must_stay_contiguous(tmp_path: Path) -> None:
    """Rows are cited by number, so a gap breaks a citation."""
    misnumbered = (
        "| 1 | The first claim, long enough to pass the length check "
        "| forecast | METR | Mar 2025 | Jul 2026 | Ch 27 |\n"
        "| 4 | The second claim, long enough to pass the length check "
        "| forecast | METR | Mar 2025 | Jul 2026 | Ch 27 |\n"
    )
    problems = validate(_tracker(misnumbered, tmp_path))

    assert [p.field for p in problems] == ["#"]
    assert "expected 2" in problems[0].message


def test_the_date_parser_handles_the_forms_the_file_uses() -> None:
    """Including the two that are not dates at all."""
    assert parse_period("Mar 2025").start == Period.index(2025, 3)
    assert parse_period("2025").start == Period.index(2025, 1)
    assert parse_period("2025").end == Period.index(2025, 12)
    assert parse_period("2025-2026").end == Period.index(2026, 12)
    assert parse_period("Sep 2025 onward").open_ended
    assert not parse_period("Sep 2025").open_ended

    with pytest.raises(ValueError, match="cannot parse date"):
        parse_period("last autumn")


def test_staleness_is_measured_against_the_per_kind_cadence() -> None:
    """Status facts age in months; measurements age per edition."""
    tracker = parse_tracker()

    assert staleness(tracker, "Jul 2026") == []
    six_months_on = staleness(tracker, "Jan 2027")
    assert six_months_on, "nothing aged over six months"
    for claim, age, allowance in six_months_on:
        assert age > allowance
        assert allowance == MAX_AGE_MONTHS[claim.kind]
    # Only the short-cadence kinds are overdue after six months.
    assert {c.kind for c, _, _ in six_months_on} <= {"status", "directional"}


def test_a_file_with_no_claims_table_is_refused(tmp_path: Path) -> None:
    """Silence is not validation."""
    path = tmp_path / "prose.md"
    path.write_text("# Just prose, no table here.\n", encoding="utf-8")

    with pytest.raises(ValueError, match="no claims table"):
        parse_tracker(path)
