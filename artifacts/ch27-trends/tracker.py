"""Validate ``trend-tracker.md``: the dated claims file at the repo root.

The Chapter 27 artifact is not a system. It is one Markdown file and the
tooling that keeps it honest, because the failure mode for a document like
that is friction rather than capability: a claims file with no checker
decays into a list of assertions nobody can date.

So this module parses the file and asserts the properties that make it
worth having:

* every claim carries a **kind**, a **source organisation**, a **source
  date**, a **verified date**, and a **book reference**;
* the kind is one of the four the chapter defines, because the whole point
  is being able to tell a measurement from a forecast on sight;
* a claim cannot have been verified before it was made;
* only a ``directional`` row may be left unverified. A ``status`` row that
  cannot be confirmed is an erratum, not a footnote.

Staleness is reported and, by default, not fatal. A repository whose test
suite starts failing four months after release because a date rolled over
teaches people to delete the check.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

__all__ = [
    "KINDS",
    "MAX_AGE_MONTHS",
    "ROT_RATES",
    "Claim",
    "Period",
    "Problem",
    "Tracker",
    "default_tracker_path",
    "parse_period",
    "parse_tracker",
    "staleness",
    "validate",
]

#: The four categories the chapter opens with. Most disagreement about
#: where agents are heading is disagreement about which of these a claim
#: belongs to.
KINDS: tuple[str, ...] = ("measurement", "forecast", "status", "directional")

#: Rot rates the rot-watch table may use.
ROT_RATES: tuple[str, ...] = ("very low", "low", "medium", "high", "very high")

#: How old a verified date may get before this artifact calls it stale, by
#: kind. Derived from the chapter's rot-watch cadences: status facts about
#: products move monthly to quarterly, measurements and forecasts are
#: per-edition claims. Reported, not enforced, unless you ask for it.
MAX_AGE_MONTHS: dict[str, int] = {
    "status": 3,
    "directional": 3,
    "measurement": 12,
    "forecast": 12,
}

#: A row that says so, rather than carrying a date it cannot support.
UNVERIFIED = "unverified"

_MONTHS = {
    name: index
    for index, name in enumerate(
        (
            "jan", "feb", "mar", "apr", "may", "jun",
            "jul", "aug", "sep", "oct", "nov", "dec",
        ),
        start=1,
    )
}

_MONTH_YEAR = re.compile(r"^([A-Za-z]{3})[a-z]*\s+(\d{4})$")
_YEAR = re.compile(r"^(\d{4})$")
_YEAR_RANGE = re.compile(r"^(\d{4})\s*-\s*(\d{4})$")
_BOOK_REF = re.compile(r"^Ch\s+\d+(\s*,\s*\d+)*$")
_PLACEHOLDERS = {"", "-", "?", "tbd", "n/a", "todo"}


@dataclass(frozen=True)
class Period:
    """A point or span in months, so two dates can be compared.

    Months are held as ``year * 12 + (month - 1)`` because the only
    operations this file needs are ordering and subtraction, and a real
    date type would invite a precision the sources do not have. "Sep 2025
    onward" is not a date; it is a half-open interval, and pretending
    otherwise is how a claims file starts lying quietly.
    """

    start: int
    end: int | None
    text: str

    @staticmethod
    def index(year: int, month: int) -> int:
        """Month index for a year and a 1-12 month."""
        return year * 12 + (month - 1)

    @property
    def open_ended(self) -> bool:
        """Whether the period has no stated end."""
        return self.end is None

    def months_before(self, other: Period) -> int:
        """How many months separate this period's start from another's."""
        return other.start - self.start

    def __str__(self) -> str:
        return self.text


def parse_period(text: str) -> Period:
    """Parse the date forms the tracker actually uses.

    Accepts ``"Mar 2025"``, ``"2025"``, ``"2025-2026"``, and
    ``"Sep 2025 onward"``.

    Raises:
        ValueError: On anything else, listing the accepted forms. A date
            the checker cannot read is a date nobody can act on.
    """
    raw = text.strip()
    body = raw
    open_ended = False
    if body.lower().endswith(" onward"):
        body = body[: -len(" onward")].strip()
        open_ended = True

    match = _MONTH_YEAR.match(body)
    if match:
        month = _MONTHS.get(match.group(1).lower())
        if month is None:
            raise ValueError(f"unknown month in {raw!r}")
        start = Period.index(int(match.group(2)), month)
        return Period(start, None if open_ended else start, raw)

    match = _YEAR_RANGE.match(body)
    if match:
        return Period(
            Period.index(int(match.group(1)), 1),
            None if open_ended else Period.index(int(match.group(2)), 12),
            raw,
        )

    match = _YEAR.match(body)
    if match:
        year = int(match.group(1))
        return Period(
            Period.index(year, 1),
            None if open_ended else Period.index(year, 12),
            raw,
        )

    raise ValueError(
        f"cannot parse date {raw!r}; expected 'Mon YYYY', 'YYYY', "
        f"'YYYY-YYYY', or any of those followed by ' onward'"
    )


@dataclass(frozen=True)
class Claim:
    """One row of the claims table, parsed."""

    number: str
    text: str
    kind: str
    source_org: str
    source_date: str
    verified: str
    book_ref: str
    line: int

    @property
    def is_unverified(self) -> bool:
        """Whether the row admits it has never been confirmed."""
        return UNVERIFIED in self.verified.lower()

    def chapters(self) -> list[int]:
        """The chapters that rely on this claim."""
        return [int(n) for n in re.findall(r"\d+", self.book_ref)]


@dataclass(frozen=True)
class Problem:
    """One thing wrong with the file, located."""

    row: str
    field: str
    message: str

    def line(self) -> str:
        """One row for the report."""
        return f"row {self.row}: {self.field}: {self.message}"


@dataclass
class Tracker:
    """The parsed file: claims, the rot-watch table, and where it came from."""

    path: Path
    claims: list[Claim] = field(default_factory=list)
    rot_rows: list[tuple[str, str, str]] = field(default_factory=list)

    def by_kind(self) -> dict[str, int]:
        """How many claims of each kind, in the order the chapter names them."""
        counts = dict.fromkeys(KINDS, 0)
        for claim in self.claims:
            if claim.kind in counts:
                counts[claim.kind] += 1
        return counts

    def chapters_covered(self) -> list[int]:
        """Every chapter that leans on at least one dated claim."""
        return sorted({c for claim in self.claims for c in claim.chapters()})


def default_tracker_path() -> Path:
    """``trend-tracker.md`` at the repository root."""
    return Path(__file__).resolve().parent.parent.parent / "trend-tracker.md"


def _cells(line: str) -> list[str]:
    """Split one Markdown table row into stripped cells."""
    return [c.strip() for c in line.strip().strip("|").split("|")]


def _is_separator(cells: list[str]) -> bool:
    """Whether a row is the ``|---|---|`` rule under a header."""
    return all(set(c) <= {"-", ":", " "} and c for c in cells)


def parse_tracker(path: Path | None = None) -> Tracker:
    """Parse the claims table and the rot-watch table out of the file.

    Raises:
        FileNotFoundError: If the tracker is missing. The book prints its
            path, so its absence is a defect rather than an option.
        ValueError: If the claims table cannot be found at all.
    """
    path = path or default_tracker_path()
    text = path.read_text(encoding="utf-8")
    tracker = Tracker(path=path)

    section = ""
    for number, raw in enumerate(text.splitlines(), start=1):
        if not raw.lstrip().startswith("|"):
            continue
        cells = _cells(raw)
        if _is_separator(cells):
            continue
        header = [c.lower() for c in cells]
        if "claim" in header and "kind" in header:
            section = "claims"
            continue
        if "layer" in header and "rot rate" in header:
            section = "rot"
            continue

        if section == "claims" and len(cells) >= 7:
            tracker.claims.append(
                Claim(
                    number=cells[0],
                    text=cells[1],
                    kind=cells[2].lower(),
                    source_org=cells[3],
                    source_date=cells[4],
                    verified=cells[5],
                    book_ref=cells[6],
                    line=number,
                )
            )
        elif section == "rot" and len(cells) >= 3:
            tracker.rot_rows.append((cells[0], cells[1].lower(), cells[2]))

    if not tracker.claims:
        raise ValueError(
            f"no claims table found in {path}; expected a Markdown table "
            f"with 'Claim' and 'kind' columns"
        )
    return tracker


def validate(tracker: Tracker) -> list[Problem]:
    """Every structural rule the claims file has to hold.

    Returns:
        A list of :class:`Problem`. Empty means the file is maintainable:
        every claim is categorised, attributed, dated, and traceable to
        the chapter that relies on it.
    """
    problems: list[Problem] = []

    for index, claim in enumerate(tracker.claims, start=1):
        row = claim.number or str(index)

        if claim.number != str(index):
            problems.append(
                Problem(row, "#", f"rows are out of order; expected {index}")
            )
        if len(claim.text) < 20:
            problems.append(
                Problem(row, "claim", "is missing or too short to check")
            )
        if claim.kind not in KINDS:
            problems.append(
                Problem(
                    row,
                    "kind",
                    f"{claim.kind!r} is not one of {', '.join(KINDS)}",
                )
            )
        if claim.source_org.lower() in _PLACEHOLDERS:
            problems.append(
                Problem(row, "source org", "a claim needs an attributable "
                                           "source, not a placeholder")
            )
        if not _BOOK_REF.match(claim.book_ref):
            problems.append(
                Problem(
                    row,
                    "book ref",
                    f"{claim.book_ref!r} should look like 'Ch 27' or "
                    f"'Ch 9, 27'",
                )
            )

        source = _period_or_problem(
            claim.source_date, row, "source date", problems
        )
        if claim.is_unverified:
            if claim.kind != "directional":
                problems.append(
                    Problem(
                        row,
                        "verified",
                        f"a {claim.kind} row cannot be left unverified; "
                        f"confirm it, demote it to directional, or remove "
                        f"it",
                    )
                )
            continue

        verified = _period_or_problem(
            claim.verified, row, "verified", problems
        )
        if source is not None and verified is not None:
            if verified.start < source.start:
                problems.append(
                    Problem(
                        row,
                        "verified",
                        f"verified {verified} predates the source date "
                        f"{source}",
                    )
                )

    problems.extend(_validate_rot(tracker))
    return problems


def _period_or_problem(
    text: str,
    row: str,
    field_name: str,
    problems: list[Problem],
) -> Period | None:
    """Parse a date, recording a problem instead of raising."""
    try:
        return parse_period(text)
    except ValueError as exc:
        problems.append(Problem(row, field_name, str(exc)))
        return None


def _validate_rot(tracker: Tracker) -> list[Problem]:
    """The rot-watch table: a rate and a cadence on every layer."""
    problems: list[Problem] = []
    if not tracker.rot_rows:
        return [
            Problem(
                "rot",
                "table",
                "no rot-watch table; the claims have no re-verification "
                "cadence",
            )
        ]
    for layer, rate, cadence in tracker.rot_rows:
        if rate not in ROT_RATES:
            problems.append(
                Problem(
                    layer,
                    "rot rate",
                    f"{rate!r} is not one of {', '.join(ROT_RATES)}",
                )
            )
        if cadence.lower() in _PLACEHOLDERS:
            problems.append(
                Problem(
                    layer,
                    "re-verify",
                    "a rot rate with no cadence is a list decaying quietly",
                )
            )
    return problems


def staleness(
    tracker: Tracker,
    as_of: str,
) -> list[tuple[Claim, int, int]]:
    """Age every verified claim against the policy in :data:`MAX_AGE_MONTHS`.

    Args:
        tracker: The parsed file.
        as_of: The date to age against, in any form
            :func:`parse_period` accepts.

    Returns:
        ``(claim, age_in_months, allowance)`` for every row that is over
        its allowance, oldest first. Reported rather than enforced: a
        stale row is a task, not a build failure.
    """
    now = parse_period(as_of)
    overdue: list[tuple[Claim, int, int]] = []
    for claim in tracker.claims:
        if claim.is_unverified:
            continue
        try:
            verified = parse_period(claim.verified)
        except ValueError:
            continue
        age = now.start - verified.start
        allowance = MAX_AGE_MONTHS.get(claim.kind, 12)
        if age > allowance:
            overdue.append((claim, age, allowance))
    return sorted(overdue, key=lambda row: -row[1])
