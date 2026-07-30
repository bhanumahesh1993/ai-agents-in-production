"""The reliability gate: pass^k over repeated runs, against a baseline.

    python -m gates.reliability --scenarios critical --k 5 \
        --min-pass-k 0.90 --baseline baselines/main.json \
        --max-regression 0.02 --version v9-good

One green run in a nondeterministic system proves nothing, so the gate
runs each critical scenario ``n`` times and reports ``pass^k`` — the
probability that ``k`` runs all succeed, which is what a customer
experiences, rather than ``pass@k``, which flatters any agent you are
willing to retry.

Two thresholds, and the second one is the one that matters. The floor
(``--min-pass-k``) says the agent must be good enough at all. The
regression bound (``--max-regression``) says the change must not be worse
than the version it replaces. Teams that gate only on a floor ship a slow
decline one acceptable step at a time.

Every run is graded against the authoritative world by the scenario's own
state grader. Nothing here reads a transcript.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent.parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from deployment import Scenario, run_once, suite_named  # noqa: E402
from northstar_evals import run_repeated  # noqa: E402
from versions import AgentVersion, version_named  # noqa: E402

__all__ = ["GateResult", "ScenarioResult", "main", "measure", "run"]

DEFAULT_BASELINE = HERE / "baselines" / "main.json"


@dataclass(frozen=True)
class ScenarioResult:
    """What repeated runs of one scenario showed, and how it compares."""

    scenario: str
    n: int
    successes: int
    pass_1: float
    pass_k: float
    ci_low: float
    ci_high: float
    baseline_pass_k: float | None
    k: int

    @property
    def regression(self) -> float:
        """How far below the baseline this landed. Negative is better."""
        if self.baseline_pass_k is None:
            return 0.0
        return self.baseline_pass_k - self.pass_k

    def verdict(
        self,
        min_pass_k: float,
        max_regression: float,
    ) -> list[str]:
        """Every reason this scenario blocks the release, named."""
        reasons: list[str] = []
        if self.pass_k < min_pass_k:
            reasons.append(
                f"pass^{self.k}={self.pass_k:.3f} below the floor "
                f"of {min_pass_k:.3f}"
            )
        if self.baseline_pass_k is not None and (
            self.regression > max_regression
        ):
            reasons.append(
                f"pass^{self.k} regressed {self.regression:.3f} against a "
                f"baseline of {self.baseline_pass_k:.3f}, over the "
                f"{max_regression:.3f} allowance"
            )
        return reasons

    def to_dict(self) -> dict[str, Any]:
        """The JSON a CI artifact carries."""
        return {
            "scenario": self.scenario,
            "n": self.n,
            "successes": self.successes,
            "pass_1": round(self.pass_1, 6),
            "pass_k": round(self.pass_k, 6),
            "k": self.k,
            "ci_low": round(self.ci_low, 6),
            "ci_high": round(self.ci_high, 6),
            "baseline_pass_k": self.baseline_pass_k,
        }


@dataclass(frozen=True)
class GateResult:
    """The gate's whole verdict, per scenario and overall."""

    version: str
    k: int
    n: int
    min_pass_k: float
    max_regression: float
    results: list[ScenarioResult]
    baseline_path: str | None

    @property
    def blocking(self) -> list[str]:
        """Every reason the release is blocked, scenario-qualified."""
        out: list[str] = []
        for result in self.results:
            for reason in result.verdict(self.min_pass_k, self.max_regression):
                out.append(f"{result.scenario}: {reason}")
        return out

    @property
    def blocked_by_regression_only(self) -> bool:
        """Whether the floor was cleared and the baseline comparison was not.

        This is the case worth knowing about: an absolute threshold a team
        might reasonably have set would have let this change through.
        """
        if not self.blocking:
            return False
        return all("regressed" in reason for reason in self.blocking)

    @property
    def passed(self) -> bool:
        """Whether the release may proceed."""
        return not self.blocking

    def to_dict(self) -> dict[str, Any]:
        """The JSON a CI artifact carries, and a baseline file's shape."""
        return {
            "version": self.version,
            "k": self.k,
            "n": self.n,
            "min_pass_k": self.min_pass_k,
            "max_regression": self.max_regression,
            "baseline": self.baseline_path,
            "passed": self.passed,
            "blocking": self.blocking,
            "scenarios": {r.scenario: r.to_dict() for r in self.results},
        }

    def report(self) -> list[str]:
        """The lines a CI log should print."""
        lines = [
            f"{'scenario':<20}{'n':>4}{'ok':>5}{'pass@1':>9}"
            f"{'pass^' + str(self.k):>9}{'ci':>16}{'baseline':>10}"
        ]
        for r in self.results:
            base = (
                f"{r.baseline_pass_k:.3f}"
                if r.baseline_pass_k is not None
                else "-"
            )
            lines.append(
                f"{r.scenario:<20}{r.n:>4}{r.successes:>5}{r.pass_1:>9.3f}"
                f"{r.pass_k:>9.3f}"
                f"{f'[{r.ci_low:.2f}, {r.ci_high:.2f}]':>16}{base:>10}"
            )
        return lines


def _task(version: AgentVersion, scenario: Scenario):  # noqa: ANN202
    """Build the callable ``run_repeated`` will invoke with a seed."""

    def run(seed: int) -> bool:
        return run_once(version, scenario, seed=seed).passed

    return run


def measure(
    version: AgentVersion,
    scenarios: tuple[Scenario, ...],
    *,
    k: int,
    n: int,
    seed: int,
    min_pass_k: float,
    max_regression: float,
    baseline: dict[str, Any] | None,
    baseline_path: str | None = None,
) -> GateResult:
    """Run the suite ``n`` times per scenario and compare with the baseline."""
    stored = (baseline or {}).get("scenarios", {})
    results: list[ScenarioResult] = []
    for scenario in scenarios:
        report = run_repeated(
            _task(version, scenario),
            n=n,
            seed=seed,
            name=scenario.name,
            k_values=(1, k),
        )
        low, high = report.interval
        prior = stored.get(scenario.name, {})
        results.append(
            ScenarioResult(
                scenario=scenario.name,
                n=report.n,
                successes=report.successes,
                pass_1=report.pass_1,
                pass_k=report.pass_k_values.get(k, 0.0),
                ci_low=low,
                ci_high=high,
                baseline_pass_k=prior.get("pass_k"),
                k=k,
            )
        )
    return GateResult(
        version=version.name,
        k=k,
        n=n,
        min_pass_k=min_pass_k,
        max_regression=max_regression,
        results=results,
        baseline_path=baseline_path,
    )


def _load_baseline(path: Path | None) -> tuple[dict[str, Any] | None, str | None]:
    """Read the stored baseline, tolerating its absence on a first run."""
    if path is None or not path.exists():
        return None, None
    return json.loads(path.read_text(encoding="utf-8")), str(path)


def build_parser() -> argparse.ArgumentParser:
    """The gate's command line, as the CI workflow invokes it."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scenarios", default="critical")
    parser.add_argument("--version", default="v9-good")
    parser.add_argument("--k", type=int, default=5)
    parser.add_argument("--n", type=int, default=20)
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--min-pass-k", type=float, default=0.90)
    parser.add_argument("--max-regression", type=float, default=0.02)
    parser.add_argument("--baseline", default=str(DEFAULT_BASELINE))
    parser.add_argument(
        "--write-baseline",
        action="store_true",
        help="record this run as the new baseline; a reviewed change",
    )
    parser.add_argument("--json", action="store_true")
    return parser


def run(argv: list[str] | None = None) -> tuple[GateResult, int]:
    """Run the gate and return its result together with the exit code.

    Split out from :func:`main` so a caller — the demo, a test, a release
    dashboard — can read the numbers instead of re-deriving them from
    stdout, and so nothing has to run the suite twice to see both.
    """
    args = build_parser().parse_args(argv)
    path = Path(args.baseline) if args.baseline else None
    baseline, baseline_path = (None, None)
    if not args.write_baseline:
        baseline, baseline_path = _load_baseline(path)

    result = measure(
        version_named(args.version),
        suite_named(args.scenarios),
        k=args.k,
        n=args.n,
        seed=args.seed,
        min_pass_k=args.min_pass_k,
        max_regression=args.max_regression,
        baseline=baseline,
        baseline_path=baseline_path,
    )

    if args.json:
        print(json.dumps(result.to_dict(), indent=2, sort_keys=True))
    else:
        print(f"reliability gate: {args.version} on suite {args.scenarios}")
        for line in result.report():
            print("  " + line)

    if args.write_baseline and path is not None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(result.to_dict(), indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        print(f"  wrote baseline to {path}")
        return result, 0

    if result.blocking:
        print("  BLOCKED:")
        for reason in result.blocking:
            print(f"    - {reason}")
        return result, 1
    print("  PASS")
    return result, 0


def main(argv: list[str] | None = None) -> int:
    """Run the gate. Returns the process exit code."""
    return run(argv)[1]


if __name__ == "__main__":
    raise SystemExit(main())
