"""The two-tier gate: replay first, then repeated simulated runs.

A replay tier that must pass at 100% catches code regressions cheaply and
precisely, and tells you nothing about this month's model. A simulated tier
with repeated runs and statistical thresholds catches behaviour regressions
and cannot be run on every commit. The gate is both, and neither half is
optional.

One operational rule prevents the most common failure of this whole
apparatus: **a run that cannot be graded is a failure, not a skip.** A missing
config hash, a truncated event log, or an absent trace means the evidence does
not exist, and a gate that quietly skips ungradeable runs will report a rising
pass rate as its own instrumentation rots. ``block_on`` in ``gate.yaml`` names
those conditions and :func:`run_gate` enforces them.

The YAML is read by a twenty-line indentation parser rather than by a
dependency, because mock mode has no runtime dependencies and a gate you
cannot run without ``pip install`` is a gate somebody will skip.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import cases
import replay as replay_tier
from cases import CASES, Case
from detectors import run_detectors
from northstar_evals import pass_k, wilson_interval

__all__ = [
    "GATE_FILE",
    "GateReport",
    "Threshold",
    "load_gate",
    "parse_yaml",
    "run_gate",
]

GATE_FILE = Path(__file__).resolve().parent / "gate.yaml"

_COMPARATORS = {
    ">=": lambda a, b: a >= b,
    "<=": lambda a, b: a <= b,
    "==": lambda a, b: a == b,
    ">": lambda a, b: a > b,
    "<": lambda a, b: a < b,
}


@dataclass(frozen=True)
class Threshold:
    """One ``">= 0.97"``-shaped condition from the gate file."""

    metric: str
    operator: str
    value: float

    @classmethod
    def parse(cls, metric: str, text: str) -> Threshold:
        """Read one threshold expression.

        Raises:
            ValueError: On an operator the gate does not understand. A
                threshold nobody can evaluate is worse than no threshold,
                because it looks like coverage.
        """
        match = re.fullmatch(r"\s*(>=|<=|==|>|<)\s*([0-9.]+)\s*", text)
        if match is None:
            raise ValueError(
                f"cannot read threshold {text!r} for {metric!r}; expected "
                'something like ">= 0.97"'
            )
        return cls(metric, match.group(1), float(match.group(2)))

    def holds(self, measured: float) -> bool:
        """Whether a measured value satisfies this condition."""
        return bool(_COMPARATORS[self.operator](measured, self.value))

    def describe(self, measured: float) -> str:
        """One line for the gate's output."""
        verdict = "ok" if self.holds(measured) else "FAIL"
        return (
            f"{self.metric:<26} {measured:>8.3f}  "
            f"{self.operator} {self.value:<6g} {verdict}"
        )


def parse_yaml(text: str) -> dict[str, Any]:
    """Read the small YAML subset ``gate.yaml`` uses.

    Nested maps, lists of scalars, and quoted or bare scalars. Anything
    else raises, because a config file that silently parses to something
    other than what it says is the exact failure this gate is meant to
    catch elsewhere.

    Raises:
        ValueError: On a line the parser cannot account for.
    """
    root: dict[str, Any] = {}
    stack: list[tuple[int, Any]] = [(-1, root)]

    for number, raw in enumerate(text.splitlines(), start=1):
        line = raw.split(" #")[0].rstrip() if " #" in raw else raw.rstrip()
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        indent = len(line) - len(line.lstrip())
        body = line.strip()

        while stack and indent <= stack[-1][0]:
            stack.pop()
        if not stack:
            raise ValueError(f"line {number}: indentation is inconsistent")
        parent = stack[-1][1]

        if body.startswith("- "):
            if not isinstance(parent, list):
                raise ValueError(
                    f"line {number}: a list item outside a list"
                )
            parent.append(_scalar(body[2:]))
            continue

        if ":" not in body:
            raise ValueError(f"line {number}: expected 'key: value'")
        key, _, value = body.partition(":")
        key, value = key.strip(), value.strip()
        if not isinstance(parent, dict):
            raise ValueError(f"line {number}: a mapping inside a list")
        if value:
            parent[key] = _scalar(value)
        else:
            child: Any = _container_for(text, number)
            parent[key] = child
            stack.append((indent, child))
    return root


def _container_for(text: str, number: int) -> Any:
    """Whether the block under line ``number`` is a list or a map."""
    for raw in text.splitlines()[number:]:
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        return [] if raw.lstrip().startswith("- ") else {}
    return {}


def _scalar(text: str) -> Any:
    """Read one scalar: quoted string, int, float, bool, or bare string."""
    value = text.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1]
    if value in ("true", "false"):
        return value == "true"
    try:
        return int(value)
    except ValueError:
        pass
    try:
        return float(value)
    except ValueError:
        return value


def load_gate(path: Path | None = None) -> dict[str, Any]:
    """Load the gate configuration."""
    return parse_yaml((path or GATE_FILE).read_text(encoding="utf-8"))


@dataclass
class GateReport:
    """Everything the gate measured, and whether it passes."""

    suite: str
    replay_pass_rate: float = 0.0
    replay_results: list[replay_tier.ReplayResult] = field(
        default_factory=list
    )
    measured: dict[str, float] = field(default_factory=dict)
    thresholds: list[Threshold] = field(default_factory=list)
    blocked: list[str] = field(default_factory=list)
    per_case: dict[str, int] = field(default_factory=dict)
    interval: tuple[float, float] = (0.0, 1.0)
    flags: list[str] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        """Whether every tier and every block condition is satisfied."""
        if self.blocked:
            return False
        return all(
            t.holds(self.measured.get(t.metric, 0.0)) for t in self.thresholds
        )


def run_gate(
    config: dict[str, Any] | None = None,
    suite: tuple[Case, ...] = CASES,
    seed: int = 1729,
) -> GateReport:
    """Run both tiers and evaluate every threshold.

    Args:
        config: Parsed gate file. Defaults to the shipped ``gate.yaml``.
        suite: Cases to run. Kept a parameter so a test can gate a subset.
        seed: Base seed for the simulated tier.

    Returns:
        A :class:`GateReport`. Nothing raises: a gate that dies on the
        first bad case cannot tell you how many other cases were bad.
    """
    cfg = config or load_gate()
    report = GateReport(suite=str(cfg.get("suite", "unnamed")))
    block_on = set(cfg.get("block_on", []))

    # ------------------------------------------------------ replay tier
    replays: list[replay_tier.ReplayResult] = []
    for case in suite:
        try:
            fixture = replay_tier.load_fixture(case.case_id)
        except (FileNotFoundError, ValueError) as exc:
            if "config_hash" in str(exc):
                report.blocked.append(
                    f"missing_config_hash: {case.case_id}"
                    if "missing_config_hash" in block_on
                    else f"unreadable fixture: {case.case_id}"
                )
            else:
                report.blocked.append(f"ungradeable_run: {case.case_id}")
            continue
        replays.append(replay_tier.replay(fixture, case))

    report.replay_results = replays
    report.replay_pass_rate = (
        sum(r.passed for r in replays) / len(replays) if replays else 0.0
    )
    required = float(cfg.get("replay", {}).get("required_pass_rate", 1.0))
    if report.replay_pass_rate < required:
        report.blocked.append(
            f"replay tier at {report.replay_pass_rate:.3f}, "
            f"required {required:.3f}"
        )

    # --------------------------------------------------- simulated tier
    simulated = cfg.get("simulated", {})
    repeats = int(simulated.get("repeats", 5))
    minimum = int(simulated.get("minimum_cases", 0))
    if len(suite) < minimum:
        report.blocked.append(
            f"suite has {len(suite)} cases, gate requires {minimum}"
        )

    outcomes: list[bool] = []
    per_case_all: list[bool] = []
    turns: list[int] = []
    violations = 0
    unauthorized = 0

    for case in suite:
        passed_here = 0
        for index in range(repeats):
            run = cases.run_case(case, seed=seed + index)
            if not run.gradeable:
                report.blocked.append(f"ungradeable_run: {case.case_id}")
                outcomes.append(False)
                continue
            grades = cases.grade(run)
            ok = all(g.passed for g in grades.values())
            outcomes.append(ok)
            passed_here += ok
            turns.append(run.state.step)
            if not grades["trajectory"].passed:
                violations += 1
            flags = run_detectors(
                run.events,
                in_scope_orders=[case.order_id],
                final_text=run.state.final_text or "",
            )
            serious = [
                f for f in flags
                if f.detector in (
                    "writes_before_authorization", "reads_outside_scope"
                )
            ]
            unauthorized += len(serious)
            report.flags += [f.describe() for f in flags]
        report.per_case[case.case_id] = passed_here
        per_case_all.append(passed_here == repeats)

    total = len(outcomes) or 1
    report.measured = {
        "outcome_success_rate": sum(outcomes) / total,
        "pass_pow_5": (
            sum(per_case_all) / len(per_case_all) if per_case_all else 0.0
        ),
        "policy_violation_rate": violations / total,
        "unauthorized_side_effects": float(unauthorized),
        "p95_turns": float(_p95(turns)),
    }
    report.interval = wilson_interval(sum(outcomes), total)
    if per_case_all:
        # pass^k over the cases themselves, as an estimator rather than a
        # bare count, so the gate's headline carries the same arithmetic
        # Chapter 13 uses.
        report.measured["pass_pow_5_estimated"] = pass_k(
            per_case_all, min(5, len(per_case_all))
        )

    report.thresholds = [
        Threshold.parse(metric, str(text))
        for metric, text in simulated.get("required", {}).items()
    ]
    return report


def _p95(values: list[int]) -> int:
    """Nearest-rank 95th percentile of an integer sample."""
    if not values:
        return 0
    ordered = sorted(values)
    index = max(0, -(-95 * len(ordered) // 100) - 1)
    return ordered[index]
