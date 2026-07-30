"""The world must produce the same bytes twice.

Every artifact in the book is offline and keyless so that a reader gets the
same result the page shows. That promise is only as good as the world's
clock. A wall clock does not merely make a timestamp vary; the timestamp
lands in a tool result, the tool result lands in the context, and
``repr(1785407178.020413)`` is a character shorter than
``repr(1785407177.953215)`` -- so the *token count* varies too, and with it
any budget or compaction decision that reads it.

Chapter 4's cost table is where this surfaced: the same pattern priced 8,478
tokens on one run and 8,480 on the next. These tests are the guard.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Any

from northstar_contracts import EPOCH, World, logical_clock
from northstar_contracts.ids import canonical_json

REPO = Path(__file__).resolve().parent.parent


def _exercise(world: World) -> dict[str, Any]:
    """Do enough to stamp every kind of timestamp the world writes."""
    world.get_order(order_id="NR-2026-0041827")
    world.issue_refund(
        order_id="NR-2026-0041827",
        amount_cents=3250,
        reason="damaged",
        idempotency_key="k1",
    )
    world.send_message(
        order_id="NR-2026-0041827", body="Your refund is on its way."
    )
    world.escalate_to_specialist(
        order_id="NR-2026-0042110", reason="fraud_review", notes="held"
    )
    return {
        "ledger": world.ledger,
        "calls": world.calls,
        "messages": world.messages,
        "escalations": world.escalations,
    }


def test_two_default_worlds_serialise_identically() -> None:
    """No argument means no wall clock. Byte-for-byte, not approximately."""
    first = canonical_json(_exercise(World()))
    second = canonical_json(_exercise(World()))
    assert first == second


def test_default_timestamps_are_whole_seconds_from_the_epoch() -> None:
    """A fractional part is a variable-length string, which is the defect."""
    world = World()
    _exercise(world)
    stamps = [row["ts"] for row in world.ledger]
    assert stamps, "the exercise wrote no ledger rows"
    assert all(float(t).is_integer() for t in stamps), stamps
    assert min(stamps) >= EPOCH


def test_logical_clock_advances_by_its_step() -> None:
    clock = logical_clock(start=100.0, step=2.0)
    assert [clock(), clock(), clock()] == [100.0, 102.0, 104.0]


def test_pattern_costs_do_not_depend_on_the_hash_seed() -> None:
    """Run Chapter 4's cost table under two hash seeds and compare.

    A separate process per seed, because ``PYTHONHASHSEED`` is read once at
    interpreter start. Set iteration order feeds prompt assembly in more
    places than one, and a token count that moves with it is a token count
    no budget can be built on.
    """
    code = (
        "import sys; sys.path.insert(0, '.');"
        " sys.path.insert(0, 'artifacts/ch04-patterns');"
        " import measure;"
        " print([(r.name, r.tokens) for r in measure.measure_all()])"
    )
    runs = []
    for seed in ("0", "12345"):
        proc = subprocess.run(
            [sys.executable, "-c", code],
            cwd=REPO,
            capture_output=True,
            text=True,
            env={"PYTHONHASHSEED": seed, "PATH": "/usr/bin:/bin"},
            timeout=300,
        )
        assert proc.returncode == 0, proc.stderr[-2000:]
        runs.append(proc.stdout.strip())
    assert runs[0] == runs[1], f"\n{runs[0]}\n{runs[1]}"
