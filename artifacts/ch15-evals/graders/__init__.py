"""The three levels of evidence, one grader each.

Outcome first, trajectory second, judge last. A judge that disagrees with the
world is wrong; a judge that agrees with the world tells you nothing new. Its
value lies in the space the other two do not cover, which is why
:func:`graders.judge.gate_dimensions` is short and why the gate applies it to
those dimensions alone.
"""

from __future__ import annotations

from graders.judge import AccuracyJudge, claims_of, gate_dimensions
from graders.state import RefundStateGrader, ledger_is_consistent
from graders.trajectory import (
    RefundPathGrader,
    before,
    distinct_orders,
    exact_match,
    tool_calls,
)

__all__ = [
    "AccuracyJudge",
    "RefundPathGrader",
    "RefundStateGrader",
    "before",
    "claims_of",
    "distinct_orders",
    "exact_match",
    "gate_dimensions",
    "ledger_is_consistent",
    "tool_calls",
]
