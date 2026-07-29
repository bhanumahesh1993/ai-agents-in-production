"""Graders, simulated users, and the repeated-run reliability harness.

The empirical half of the book. Nothing here needs a network or a key: the
judge is deterministic in mock mode and the simulated users are scripts::

    from northstar_evals import StateGrader, TrajectoryGrader, run_repeated
"""

from __future__ import annotations

from .graders import (
    DEFAULT_RUBRIC,
    Grader,
    GradeResult,
    JudgeGrader,
    StateGrader,
    TrajectoryGrader,
    grade_all,
    observations_of,
    tool_calls_of,
    trajectory,
)
from .reliability import (
    ReliabilityReport,
    pass_at_k,
    pass_k,
    run_repeated,
    wilson_interval,
)
from .simulated_user import PERSONAS, Persona, SimulatedUser, Turn

__version__ = "1.0.0"

__all__ = [
    "DEFAULT_RUBRIC",
    "PERSONAS",
    "GradeResult",
    "Grader",
    "JudgeGrader",
    "Persona",
    "ReliabilityReport",
    "SimulatedUser",
    "StateGrader",
    "TrajectoryGrader",
    "Turn",
    "__version__",
    "grade_all",
    "observations_of",
    "pass_at_k",
    "pass_k",
    "run_repeated",
    "tool_calls_of",
    "trajectory",
    "wilson_interval",
]
