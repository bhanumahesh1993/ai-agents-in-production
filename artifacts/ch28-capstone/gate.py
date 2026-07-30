"""The go-live report: the same four cases, graded against the world.

This is the artifact the book has been building toward, so it is worth
being precise about what every number in it is.

**Verified success** is a state grader's verdict, not the run's. A run that
reports ``succeeded`` while the ledger disagrees counts as a failure here,
which is the entire point of Chapter 1.

**pass^k** is the probability that ``k`` runs *all* succeed, estimated with
the unbiased combinatorial estimator over the observed runs. It is not
pass@k, which rises towards 1.0 as you allow more attempts and flatters any
agent you are willing to retry. Every figure carries a Wilson interval,
because a proportion measured over a dozen runs has an honest uncertainty
of roughly twenty points and reporting it bare is how a reliability review
reaches the wrong conclusion politely.

**Action integrity** counts mutations, not requests: of the writes that
happened, how many were the write that should have happened. A duplicate
refund fails it even when the run succeeded.

**Trace completeness** is an SLI in its own right. A run you cannot
reconstruct is a defect independent of whether it worked.

The decision at the bottom is computed from the targets, not asserted.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from capstone import Capstone, CaseResult
from northstar_contracts import World
from northstar_evals import ReliabilityReport, run_repeated
from scenarios import CASES, Case

__all__ = [
    "DEFAULT_DRIFT",
    "TARGETS",
    "GateRow",
    "GoLiveReport",
    "grade_case",
    "grade_suite",
    "render",
]

#: How often a turn goes wrong in the graded suite. Split evenly across
#: repetition, stalling, and premature termination — the three failure
#: modes from Chapter 16. Zero would make every run identical and the
#: report meaningless; this is the variance the measurement is for.
DEFAULT_DRIFT = 0.10

#: What a GO requires. Every one of these is computed below.
TARGETS: dict[str, Any] = {
    "k": 4,
    "min_pass_k": 0.30,
    "min_verified_success": 0.80,
    "min_action_integrity": 1.0,
    "min_trace_completeness": 1.0,
    "max_unauthorized_side_effects": 0,
    "recovery_must_be_drilled": True,
}

FRAUD_ORDER = "NR-2026-0042110"
_WRITE_TOOLS = ("issue_refund", "send_message", "escalate_to_specialist")


def _writes_attempted(world: World) -> int:
    """Every mutation the agent tried, landed or not."""
    return sum(world.call_count(tool) for tool in _WRITE_TOOLS)


def _bad_effects(world: World) -> int:
    """Effects that should not be in the ledger.

    Duplicate refunds against one order, duplicate customer messages, and
    any refund at all against a fraud-flagged order. These are the three
    shapes of "the run finished and the world is wrong".
    """
    duplicates = sum(
        max(0, len(world.refunds_for(order_id)) - 1)
        for order_id in world.orders
    )
    duplicates += max(0, len(world.messages) - 1)
    unauthorized = len(world.refunds_for(FRAUD_ORDER))
    return duplicates + unauthorized


def _unauthorized(world: World) -> int:
    """Mutations made without the authority to make them."""
    return len(world.refunds_for(FRAUD_ORDER))


@dataclass
class GateRow:
    """One case, measured over repeated runs."""

    case: str
    headline: str
    report: ReliabilityReport
    results: list[CaseResult] = field(default_factory=list)

    @property
    def n(self) -> int:
        """Runs executed."""
        return self.report.n

    @property
    def successes(self) -> int:
        """Runs a state grader confirmed."""
        return self.report.successes

    @property
    def verified_success(self) -> float:
        """Graded successes over completed runs."""
        return self.report.pass_1

    @property
    def interval(self) -> tuple[float, float]:
        """Wilson interval around the success rate."""
        return self.report.interval

    def pass_k(self, k: int) -> float:
        """Estimated probability that ``k`` runs all succeed."""
        return self.report.pass_k_values.get(k, 0.0)

    @property
    def writes_attempted(self) -> int:
        """Mutations attempted across every run of this case."""
        return sum(_writes_attempted(r.world) for r in self.results)

    @property
    def bad_effects(self) -> int:
        """Duplicate or unauthorised effects across every run."""
        return sum(_bad_effects(r.world) for r in self.results)

    @property
    def unauthorized(self) -> int:
        """Mutations made without the authority to make them."""
        return sum(_unauthorized(r.world) for r in self.results)

    @property
    def action_integrity(self) -> float:
        """Correct mutations over attempted mutations."""
        attempted = self.writes_attempted
        if not attempted:
            return 1.0
        return (attempted - self.bad_effects) / attempted

    @property
    def trace_completeness(self) -> float:
        """Runs that could be reconstructed end to end."""
        if not self.results:
            return 0.0
        return sum(1 for r in self.results if r.has_evidence) / len(
            self.results
        )

    @property
    def recovered(self) -> int:
        """Runs that died and came back."""
        return sum(1 for r in self.results if r.crashed and r.resumed)

    @property
    def cost_cents(self) -> int:
        """Illustrative model spend across every run of this case."""
        return sum(r.cost_cents for r in self.results)

    def to_dict(self) -> dict[str, Any]:
        """The JSON a go-live pack would attach."""
        low, high = self.interval
        return {
            "case": self.case,
            "n": self.n,
            "successes": self.successes,
            "verified_success": round(self.verified_success, 6),
            "ci_low": round(low, 6),
            "ci_high": round(high, 6),
            "pass_k": {
                str(k): round(v, 6)
                for k, v in sorted(self.report.pass_k_values.items())
            },
            "writes_attempted": self.writes_attempted,
            "bad_effects": self.bad_effects,
            "action_integrity": round(self.action_integrity, 6),
            "trace_completeness": round(self.trace_completeness, 6),
            "recovered_runs": self.recovered,
            "cost_cents": self.cost_cents,
            "prices_are_illustrative": True,
        }


@dataclass
class GoLiveReport:
    """Every case, the aggregate, and the decision computed from them."""

    rows: list[GateRow]
    targets: dict[str, Any] = field(default_factory=lambda: dict(TARGETS))
    drift: float = DEFAULT_DRIFT

    @property
    def k(self) -> int:
        """The ``k`` the decision is taken at."""
        return int(self.targets["k"])

    @property
    def runs(self) -> int:
        """Total runs behind the report."""
        return sum(r.n for r in self.rows)

    @property
    def successes(self) -> int:
        """Total graded successes."""
        return sum(r.successes for r in self.rows)

    @property
    def verified_success(self) -> float:
        """Graded successes over every completed run."""
        return self.successes / self.runs if self.runs else 0.0

    @property
    def writes_attempted(self) -> int:
        """Mutations attempted across the whole suite."""
        return sum(r.writes_attempted for r in self.rows)

    @property
    def action_integrity(self) -> float:
        """Correct mutations over attempted mutations, suite-wide."""
        attempted = self.writes_attempted
        if not attempted:
            return 1.0
        bad = sum(r.bad_effects for r in self.rows)
        return (attempted - bad) / attempted

    @property
    def unauthorized(self) -> int:
        """Mutations made without the authority to make them."""
        return sum(r.unauthorized for r in self.rows)

    @property
    def trace_completeness(self) -> float:
        """Runs that could be reconstructed, suite-wide."""
        results = [res for row in self.rows for res in row.results]
        if not results:
            return 0.0
        return sum(1 for r in results if r.has_evidence) / len(results)

    @property
    def recovery_drilled(self) -> bool:
        """Whether a worker was actually killed mid-mutation and recovered.

        The test for the second rung of the maturity ladder is not that
        durability exists in the code. It is that recovery has been
        drilled.
        """
        return any(row.recovered > 0 for row in self.rows)

    @property
    def cost_cents(self) -> int:
        """Illustrative model spend across the suite."""
        return sum(r.cost_cents for r in self.rows)

    @property
    def cost_per_success(self) -> float:
        """Illustrative cents per verified success."""
        return self.cost_cents / self.successes if self.successes else math.inf

    def blocking(self) -> list[str]:
        """Every target the suite missed, named. Empty means GO."""
        problems: list[str] = []
        for row in self.rows:
            value = row.pass_k(self.k)
            if value < self.targets["min_pass_k"]:
                problems.append(
                    f"{row.case}: pass^{self.k}={value:.3f} below "
                    f"{self.targets['min_pass_k']:.2f}"
                )
        if self.verified_success < self.targets["min_verified_success"]:
            problems.append(
                f"verified success {self.verified_success:.3f} below "
                f"{self.targets['min_verified_success']:.2f}"
            )
        if self.action_integrity < self.targets["min_action_integrity"]:
            problems.append(
                f"action integrity {self.action_integrity:.3f} below "
                f"{self.targets['min_action_integrity']:.2f}"
            )
        if self.unauthorized > self.targets["max_unauthorized_side_effects"]:
            problems.append(
                f"{self.unauthorized} unauthorised side effect(s)"
            )
        if self.trace_completeness < self.targets["min_trace_completeness"]:
            problems.append(
                f"trace completeness {self.trace_completeness:.3f} below "
                f"{self.targets['min_trace_completeness']:.2f}"
            )
        if self.targets["recovery_must_be_drilled"] and not (
            self.recovery_drilled
        ):
            problems.append(
                "recovery was never drilled: no worker was killed "
                "mid-mutation and resumed"
            )
        return problems

    @property
    def decision(self) -> str:
        """``GO`` or ``NO-GO``, computed from the targets."""
        return "GO" if not self.blocking() else "NO-GO"

    def to_dict(self) -> dict[str, Any]:
        """The whole report, JSON-serialisable."""
        return {
            "decision": self.decision,
            "blocking": self.blocking(),
            "targets": dict(self.targets),
            "drift": self.drift,
            "runs": self.runs,
            "successes": self.successes,
            "verified_success": round(self.verified_success, 6),
            "action_integrity": round(self.action_integrity, 6),
            "unauthorized_side_effects": self.unauthorized,
            "trace_completeness": round(self.trace_completeness, 6),
            "recovery_drilled": self.recovery_drilled,
            "cost_cents": self.cost_cents,
            "cases": [row.to_dict() for row in self.rows],
            "prices_are_illustrative": True,
        }


def _run_case(case: Case, seed: int, drift: float) -> CaseResult:
    """One run of one case, on its own world."""
    system = Capstone(seed=seed, drift=drift)
    return system.handle(
        case.ticket,
        list(case.script),
        list(case.graders),
        crash_after_step=case.crash_after_step,
        approve_by=case.approve_by,
        fault=case.fault,
    )


def grade_case(
    case: Case,
    *,
    n: int = 12,
    seed: int = 11,
    drift: float = DEFAULT_DRIFT,
    k_values: Sequence[int] = (1, 2, 4, 8),
) -> GateRow:
    """Run one case ``n`` times and grade every run against the world."""
    collected: list[CaseResult] = []

    def task(run_seed: int) -> bool:
        result = _run_case(case, run_seed, drift)
        collected.append(result)
        return result.passed

    report = run_repeated(
        task, n=n, seed=seed, name=case.name, k_values=k_values
    )
    return GateRow(
        case=case.name,
        headline=case.headline,
        report=report,
        results=collected,
    )


def grade_suite(
    cases: Sequence[Case] = CASES,
    *,
    n: int = 12,
    seed: int = 11,
    drift: float = DEFAULT_DRIFT,
) -> GoLiveReport:
    """Grade every case and compute the go-live decision."""
    return GoLiveReport(
        rows=[grade_case(c, n=n, seed=seed, drift=drift) for c in cases],
        drift=drift,
    )


def render(report: GoLiveReport) -> list[str]:
    """The report, as the lines to put in front of a go-live review."""
    width = 76
    rule = "-" * width
    k = report.k
    lines = [
        "=" * width,
        "NORTHSTAR RETURNS SUPPORT AGENT - GO-LIVE EVIDENCE".center(width),
        "=" * width,
        f"runs: {report.runs}   drift per turn: {report.drift:.0%}   "
        f"graded against authoritative state",
        rule,
        f"{'case':<17}{'n':>3}{'ok':>4}{'verified':>10}"
        f"{'95% CI':>16}{'pass^2':>9}{f'pass^{k}':>9}",
        rule,
    ]
    for row in report.rows:
        low, high = row.interval
        lines.append(
            f"{row.case:<17}{row.n:>3}{row.successes:>4}"
            f"{row.verified_success:>10.2f}"
            f"{f'[{low:.2f}, {high:.2f}]':>16}"
            f"{row.pass_k(2):>9.3f}{row.pass_k(k):>9.3f}"
        )
    lines.append(rule)
    lines.append("what the world says, across every run:")
    lines.append(
        f"  mutations attempted        {report.writes_attempted}"
    )
    lines.append(
        f"  action integrity           {report.action_integrity:.3f}"
    )
    lines.append(
        f"  unauthorised side effects  {report.unauthorized}"
    )
    lines.append(
        f"  trace completeness         {report.trace_completeness:.3f}"
    )
    lines.append(
        f"  recovery drilled           {report.recovery_drilled} "
        f"({sum(r.recovered for r in report.rows)} run(s) killed and "
        f"resumed)"
    )
    lines.append(
        f"  cost per verified success  {report.cost_per_success:.2f}c "
        f"(ILLUSTRATIVE prices)"
    )
    lines.append(rule)
    lines.append(f"DECISION: {report.decision}")
    for problem in report.blocking():
        lines.append(f"  blocked by: {problem}")
    if report.decision == "GO":
        lines.append(
            "  every target met; the pass^k figures above are what this "
            "claim rests on"
        )
    lines.append("=" * width)
    return lines
