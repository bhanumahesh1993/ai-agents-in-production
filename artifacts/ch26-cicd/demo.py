"""Run the whole release pipeline against a mock deployment, offline.

    python artifacts/ch26-cicd/demo.py

Six sections, in the order a change moves through them:

1. **Versioning.** The effective configuration hash over code, model
   snapshot, prompt, tool versions, policy, guardrails, and sandbox image.
   A prompt-only edit changes it, which is the point.
2. **Reliability gate.** ``pass^k`` over repeated runs, blocked on
   regression against a stored baseline rather than on an absolute score.
   Three invocations: a good candidate passes, a regressed one is blocked,
   and a marginal one clears an absolute floor and is blocked anyway.
3. **Trajectory gate.** Invariants rather than an exact path. The unsafe
   candidate reaches the same world state by the wrong route and with no
   idempotency key, and is blocked for both.
4. **Shadow traffic.** Two versions run on the same input with every write
   recorded and none executed, then diffed on the decisions they would
   have made.
5. **Canary.** Reads before writes, bounded writes before general ones,
   widened while the SLOs hold and contained by an independent flag when
   they do not.
6. **Kill-switch drill.** Every rung of the containment ladder pulled, and
   what each one actually achieved measured — including the one that
   stops new work and leaves in-flight runs mutating.

Exits non-zero if a gate fails to block a known-bad candidate, if it
blocks a good one, if a shadowed run touches the world, if the canary
widens past a breach, or if any rung of the containment ladder does not do
what it claims.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from canary import FlagSet  # noqa: E402
from deployment import CRITICAL, DAMAGED_REFUND  # noqa: E402
from drill import drill_all, in_flight_containment  # noqa: E402
from gates import reliability, trajectory  # noqa: E402
from northstar_contracts import World  # noqa: E402
from rollout import walk  # noqa: E402
from shadow import compare, shadow_run  # noqa: E402
from versions import (  # noqa: E402
    RELEASE_ARTIFACTS,
    V8,
    V9_GENEROUS,
    V9_GOOD,
    V9_MARGINAL,
    V9_REGRESSED,
    V9_UNSAFE,
)

BASELINE = str(Path(__file__).resolve().parent / "baselines" / "main.json")
INVARIANTS = [
    "issue_refund before get_policy",
    "issue_refund without idempotency_key",
]


def section(title: str) -> None:
    """Print one section header."""
    print(f"\n=== {title} ===")


def show_versions(failures: list[str]) -> None:
    """The configuration hash, and what it covers."""
    section("everything that can change behaviour, hashed")
    specs = World().tool_specs()
    print(f"release artifacts covered: {len(RELEASE_ARTIFACTS)}")
    print("  " + ", ".join(RELEASE_ARTIFACTS[:6]))
    print("  " + ", ".join(RELEASE_ARTIFACTS[6:]))
    for version in (V8, V9_GOOD, V9_UNSAFE):
        print(
            f"  {version.name:<14} {version.short_config_hash(specs)}  "
            f"{version.note}"
        )
    if V8.short_config_hash(specs) == V9_GOOD.short_config_hash(specs):
        failures.append(
            "a prompt-only edit did not change the configuration hash"
        )


def run_reliability_gate(
    version: str,
    *,
    min_pass_k: float,
    label: str,
) -> reliability.GateResult:
    """Invoke the gate exactly as the CI workflow does, and print it."""
    print(f"\n  {label}")
    result, _code = reliability.run(
        [
            "--scenarios", "critical",
            "--k", "5",
            "--min-pass-k", str(min_pass_k),
            "--baseline", BASELINE,
            "--max-regression", "0.02",
            "--version", version,
        ]
    )
    return result


def show_reliability(failures: list[str]) -> None:
    """Three invocations of one gate."""
    section("reliability gate: pass^k against a stored baseline")
    good = run_reliability_gate(
        V9_GOOD.name, min_pass_k=0.90, label="candidate v9-good"
    )
    regressed = run_reliability_gate(
        V9_REGRESSED.name, min_pass_k=0.90, label="candidate v9-regressed"
    )
    marginal = run_reliability_gate(
        V9_MARGINAL.name,
        min_pass_k=0.30,
        label="candidate v9-marginal, against a 0.30 absolute floor",
    )

    if not good.passed:
        failures.append("the reliability gate blocked a good candidate")
    if regressed.passed:
        failures.append("the reliability gate passed a regressed candidate")
    if marginal.passed:
        failures.append("the reliability gate passed a marginal candidate")
    elif not marginal.blocked_by_regression_only:
        failures.append(
            "the marginal candidate should have been blocked by the "
            "baseline comparison alone"
        )
    else:
        print(
            "\n  v9-marginal cleared the absolute floor and was blocked by "
            "the baseline\n  comparison alone. That is the case an absolute "
            "threshold lets through."
        )


def show_trajectory(failures: list[str]) -> None:
    """Invariants, checked against what the tool actually received."""
    section("trajectory gate: invariants, not an exact path")
    for version, should_pass in ((V9_GOOD, True), (V9_UNSAFE, False)):
        args = [
            *sum((["--forbid", i] for i in INVARIANTS), []),
            "--require-state-grader",
            "--version", version.name,
        ]
        print(f"\n  candidate {version.name}")
        passed = trajectory.main(args) == 0
        if passed != should_pass:
            failures.append(
                f"the trajectory gate returned {passed} for {version.name}"
            )


def show_shadow(failures: list[str]) -> None:
    """Decisions compared without a single write executed."""
    section("shadow traffic: recorded intents, zero side effects")
    baseline = shadow_run(V8, DAMAGED_REFUND)
    candidate = shadow_run(V9_GENEROUS, DAMAGED_REFUND)
    diff = compare(baseline, candidate)

    for run in (baseline, candidate):
        print(
            f"  {run.version:<14} write intents={run.write_intents} "
            f"side effects in the world={run.side_effects}"
        )
    for line in diff.report():
        print(f"  {line}")

    if baseline.side_effects or candidate.side_effects:
        failures.append("a shadowed run changed the world")
    if not baseline.write_intents or not candidate.write_intents:
        failures.append("the shadow adapter dropped a write instead of "
                        "recording it")
    if diff.identical:
        failures.append("the shadow diff missed a changed decision")


def show_canary(failures: list[str]) -> None:
    """Widen while the SLOs hold; contain with a flag when they do not."""
    section("canary: reads, then bounded writes, then general writes")
    for version, expect_complete in ((V9_GOOD, True), (V9_GENEROUS, False)):
        controller, observations = walk(version)
        outcome = (
            "reached full exposure"
            if controller.complete
            else "contained without a deploy"
        )
        print(f"\n  candidate {version.name}: {outcome}")
        for observation in observations:
            print(f"    {observation.line()}")
            for note in observation.notes[:2]:
                print(f"      note: {note}")
        for action in controller.flags.actions:
            print(
                f"    flag pulled: {action['flag']} "
                f"({action['reason']})"
            )
        if controller.complete != expect_complete:
            failures.append(
                f"canary on {version.name}: complete={controller.complete}"
            )
        if not expect_complete and controller.flags.enabled("all_mutations"):
            failures.append(
                "containment did not disable mutations on a breach"
            )


def show_drill(failures: list[str]) -> None:
    """Pull every rung, and measure what each one achieved."""
    section("kill-switch drill: every rung, measured")
    for result in drill_all():
        print(f"  {result.line()}")
        if not result.passed:
            failures.append(f"drill failed for {result.flag}")

    enforced = in_flight_containment(enforce=True)
    naive = in_flight_containment(enforce=False)
    print(
        f"\n  in-flight runs: {enforced['runs_in_flight']} mid-trajectory "
        f"when the switch was pulled"
    )
    print(
        f"    flags at the action boundary : "
        f"{enforced['mutated_after_flip']} mutation(s) after the flip"
    )
    print(
        f"    flags at admission only      : "
        f"{naive['mutated_after_flip']} mutation(s) after the flip"
    )
    if enforced["mutated_after_flip"] != 0:
        failures.append("containment did not stop in-flight mutation")
    if naive["mutated_after_flip"] == 0:
        failures.append(
            "the naive containment should have kept mutating; the drill "
            "is no longer demonstrating anything"
        )


def main() -> int:
    failures: list[str] = []
    print(f"critical suite: {[s.name for s in CRITICAL]}")
    print(f"baseline      : {BASELINE}")
    print(f"flags         : {sorted(FlagSet().values)}")

    show_versions(failures)
    show_reliability(failures)
    show_trajectory(failures)
    show_shadow(failures)
    show_canary(failures)
    show_drill(failures)

    print("\n--- what this proves ---")
    print("A behavioural regression is blocked before release by gating on")
    print("repeated-run reliability and trajectory invariants against a")
    print("baseline. A candidate's decisions are compared with production")
    print("without executing one write. And every rung of the containment")
    print("ladder is exercised without a deploy and verified to stop")
    print("in-flight mutation.")

    if failures:
        print("\nFAILED:")
        for line in failures:
            print(f"  - {line}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
