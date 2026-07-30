"""The trajectory gate: invariants, not an exact path.

    python -m gates.trajectory \
        --forbid "issue_refund before get_policy" \
        --forbid "issue_refund without idempotency_key" \
        --require-state-grader --version v9-unsafe

The forbidden-transition list is the useful part. It asserts what must
never be true rather than what must happen in what order, which is what
lets the gate survive a legitimate change in the agent's route: an agent
that reads the order twice, or reads the policy before the order, is
taking a different valid path and should not fail a release.

Two invariant forms are understood:

``"A before B"``
    Forbid A happening before the first B. Checked against the trajectory
    recovered from the run's messages.

``"A without ARG"``
    Forbid a call to A whose *effective* arguments lack ARG. Effective is
    the word that matters: a derived idempotency key is stamped by the
    runtime, not written by the model, so this invariant cannot be
    checked against the transcript. It is checked against what the tool
    actually received.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent.parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from deployment import RunOutcome, Scenario, run_once, suite_named  # noqa: E402
from northstar_evals import TrajectoryGrader, trajectory  # noqa: E402
from versions import AgentVersion, version_named  # noqa: E402

__all__ = [
    "Invariant",
    "TrajectoryGateResult",
    "check",
    "main",
    "parse_invariant",
    "run",
]

_BEFORE = re.compile(r"^\s*(\w+)\s+before\s+(\w+)\s*$")
_WITHOUT = re.compile(r"^\s*(\w+)\s+without\s+(\w+)\s*$")


@dataclass(frozen=True)
class Invariant:
    """One forbidden pattern, parsed from the command line."""

    kind: str
    tool: str
    other: str
    source: str

    def describe(self) -> str:
        """The invariant as the operator wrote it."""
        return self.source


def parse_invariant(text: str) -> Invariant:
    """Parse ``"A before B"`` or ``"A without ARG"``.

    Raises:
        ValueError: On anything else, naming both accepted forms. A gate
            that silently ignores an invariant it could not parse is a
            gate that reports green for the wrong reason.
    """
    match = _BEFORE.match(text)
    if match:
        return Invariant("before", match.group(1), match.group(2), text)
    match = _WITHOUT.match(text)
    if match:
        return Invariant("without", match.group(1), match.group(2), text)
    raise ValueError(
        f"cannot parse invariant {text!r}; expected "
        f'"<tool> before <tool>" or "<tool> without <argument>"'
    )


@dataclass(frozen=True)
class TrajectoryGateResult:
    """Per-scenario violations, and the overall verdict."""

    version: str
    invariants: list[str]
    violations: list[str]
    trajectories: dict[str, list[str]]
    state_graders: dict[str, bool]

    @property
    def passed(self) -> bool:
        """Whether the release may proceed."""
        return not self.violations

    def to_dict(self) -> dict[str, Any]:
        """The JSON a CI artifact carries."""
        return {
            "version": self.version,
            "invariants": list(self.invariants),
            "violations": list(self.violations),
            "trajectories": {k: list(v) for k, v in self.trajectories.items()},
            "state_graders": dict(self.state_graders),
            "passed": self.passed,
        }


def _violations_for(
    outcome: RunOutcome,
    invariant: Invariant,
) -> list[str]:
    """Every way one run breaks one invariant."""
    path = trajectory(outcome.state)
    if invariant.kind == "before":
        # "A before B" is forbidden, so B must precede A wherever A runs.
        grader = TrajectoryGrader(before=[(invariant.other, invariant.tool)])
        grade = grader.grade(outcome.state, outcome.world)
        if grade.passed:
            return []
        return [
            f"{invariant.source}: trajectory was {path}",
        ]

    missing = [
        d
        for d in outcome.tools.dispatched
        if d["tool"] == invariant.tool
        and not d["arguments"].get(invariant.other)
    ]
    if not missing:
        return []
    return [
        f"{invariant.source}: {len(missing)} call(s) reached the tool "
        f"without {invariant.other}"
    ]


def check(
    version: AgentVersion,
    scenarios: tuple[Scenario, ...],
    invariants: list[Invariant],
    *,
    require_state_grader: bool = True,
) -> TrajectoryGateResult:
    """Run each scenario once, deterministically, and check the invariants.

    Deterministic on purpose. A trajectory invariant is a property of the
    route the agent takes, and measuring it under injected flakiness
    conflates two questions. Reliability is the other gate's job.
    """
    violations: list[str] = []
    paths: dict[str, list[str]] = {}
    graders: dict[str, bool] = {}

    for scenario in scenarios:
        outcome = run_once(version, scenario, deterministic=True)
        paths[scenario.name] = trajectory(outcome.state)
        # A grader with no checks reports "no checks configured" and
        # carries no details. That is the shape this looks for, because a
        # scenario whose grader asserts nothing passes everything.
        grade = scenario.grader.grade(outcome.state, outcome.world)
        has_grader = int(grade.details.get("checks", 0)) > 0
        graders[scenario.name] = has_grader
        if require_state_grader and not has_grader:
            violations.append(
                f"{scenario.name}: no state grader is declared for this "
                f"scenario, so nothing reads the world"
            )
        for invariant in invariants:
            for problem in _violations_for(outcome, invariant):
                violations.append(f"{scenario.name}: {problem}")

    return TrajectoryGateResult(
        version=version.name,
        invariants=[i.source for i in invariants],
        violations=violations,
        trajectories=paths,
        state_graders=graders,
    )


def build_parser() -> argparse.ArgumentParser:
    """The gate's command line, as the CI workflow invokes it."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scenarios", default="critical")
    parser.add_argument("--version", default="v9-good")
    parser.add_argument(
        "--forbid",
        action="append",
        default=[],
        metavar="INVARIANT",
        help='e.g. "issue_refund before get_policy"',
    )
    parser.add_argument("--require-state-grader", action="store_true")
    parser.add_argument("--json", action="store_true")
    return parser


def run(argv: list[str] | None = None) -> tuple[TrajectoryGateResult, int]:
    """Run the gate and return its result together with the exit code."""
    args = build_parser().parse_args(argv)
    invariants = [parse_invariant(text) for text in args.forbid]
    result = check(
        version_named(args.version),
        suite_named(args.scenarios),
        invariants,
        require_state_grader=args.require_state_grader,
    )

    if args.json:
        print(json.dumps(result.to_dict(), indent=2, sort_keys=True))
    else:
        print(f"trajectory gate: {args.version} on suite {args.scenarios}")
        for name, path in result.trajectories.items():
            print(f"  {name:<20} {' -> '.join(path) or '(no tool calls)'}")

    if result.violations:
        print("  BLOCKED:")
        for reason in result.violations:
            print(f"    - {reason}")
        return result, 1
    print("  PASS")
    return result, 0


def main(argv: list[str] | None = None) -> int:
    """Run the gate. Returns the process exit code."""
    return run(argv)[1]


if __name__ == "__main__":
    raise SystemExit(main())
