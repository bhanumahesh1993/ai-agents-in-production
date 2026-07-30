# Chapter 27 — a claims file you can actually maintain

**What this artifact proves:** a book about a fast-moving field stays
maintainable by keeping volatile claims out of its prose. Every dated assertion
in Chapter 27 is one row in `trend-tracker.md` at the repository root, carrying
its category, its source organisation, its source date, the date it was last
verified, and the chapters that rely on it — so a reader can determine what to
distrust without re-deriving it, and a future edition refreshes the file rather
than the chapter.

The artifact itself is the tooling that keeps that file honest. The chapter says
the file has no tooling around it because the failure mode for a document like
this is friction rather than capability. That is still true of *maintaining* it:
you edit Markdown. What is here is a checker, and a checker is not friction —
it is the thing that notices when a row has quietly stopped carrying its
attribution.

## Run it

```bash
make demo-ch27
# or
python artifacts/ch27-trends/demo.py
python artifacts/ch27-trends/demo.py --as-of "Jan 2027" --strict-age
```

The demo does three things:

1. **Validates the real file.** Every claim categorised, attributed, dated, and
   traceable to a chapter; no claim verified before it was made; nothing left
   unverified except the rows the chapter deliberately declined to assert.
2. **Ages it.** Staleness against a per-kind cadence derived from the chapter's
   rot-watch table — three months for `status` and `directional`, twelve for
   `measurement` and `forecast`. Reported, never fatal unless you pass
   `--strict-age`. A check that starts failing because a date rolled over is a
   check people delete.
3. **Falsifies itself.** Runs the same validator over a file with one of each
   defect planted in it and confirms every one is caught. A validator nobody has
   watched fail is a validator that might be returning green for the wrong
   reason.

It exits non-zero if the real file has a structural problem or if any planted
defect goes uncaught.

## The rules, and why each one is there

| Rule | Why |
|---|---|
| `kind` is one of `measurement`, `forecast`, `status`, `directional` | Most disagreement about where agents are heading is disagreement about which of these a claim is. A forecast that turns out wrong is not an erratum; a status fact that is wrong *is*. |
| A source organisation, not a placeholder | An unattributed number is a rumour with a table cell around it. |
| A parseable source date | `Mar 2025`, `2025`, `2025-2026`, and `Sep 2025 onward` are the forms the file uses. Two of those are not dates, and the parser says so rather than flattening them. |
| A verified date, or an explicit `**unverified**` | A blank is indistinguishable from an oversight. |
| Only `directional` rows may be unverified | A `status` row that cannot be confirmed against a primary source gets demoted or removed. It does not get quietly kept. |
| Verified cannot predate the source | The cheapest inconsistency to introduce by hand while editing a table, and the cheapest to catch. |
| A book reference like `Ch 27` or `Ch 9, 27` | The row has to be findable from the prose that leans on it, and vice versa. |
| Every rot-watch layer has a rate and a cadence | A watchlist with no schedule is a list of claims decaying quietly. |

## Files

| File | What it is |
|---|---|
| `../../trend-tracker.md` | **The artifact.** The claims table and the rot-watch table. Edit this one. |
| `tracker.py` | Parses both tables, `parse_period()` for the date forms the file uses, `validate()` for the rules above, and `staleness()` for the ageing report. |
| `demo.py` | Validates the real file, ages it, and proves the checker bites. |
| `test_ch27.py` | The same properties as assertions, plus the parser's edge cases. |

## The maintenance loop

Four steps, designed to be done in an afternoon:

1. Work down the table and open each source.
2. If the claim holds, update `verified` to today.
3. If it does not, add a new row and mark the old one superseded. Do not edit
   history.
4. If it cannot be re-verified against a primary source, demote it to
   `directional` or delete it.

Then run `python artifacts/ch27-trends/demo.py --as-of "<this month>"` and fix
whatever it names. The ageing report tells you which rows to open first.
