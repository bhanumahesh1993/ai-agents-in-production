"""The simulator: named world fixtures and scripted personas.

Five parts sit around the agent under test — a simulated user, a fake world,
a fault injector, the graders, and a report. Two of them live here; the
graders live in ``graders/`` and the fault injector is ``World.inject_fault``,
which the case set drives from a declared fault schedule.
"""

from __future__ import annotations

from sim.personas import (
    ASKS_TO_IGNORE_INSTRUCTIONS,
    GOES_SILENT,
    PERSONAS,
    WANTS_FULL_REFUND,
    WITHHOLDS_ORDER_ID,
    WRONG_ORDER_CORRECTS,
    SimulatedUser,
)
from sim.world import FIXTURES, from_fixture

__all__ = [
    "ASKS_TO_IGNORE_INSTRUCTIONS",
    "FIXTURES",
    "GOES_SILENT",
    "PERSONAS",
    "WANTS_FULL_REFUND",
    "WITHHOLDS_ORDER_ID",
    "WRONG_ORDER_CORRECTS",
    "SimulatedUser",
    "from_fixture",
]
