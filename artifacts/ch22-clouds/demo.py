"""One agent, one scorecard, no cloud account.

    python artifacts/ch22-clouds/demo.py

Runs the full scorecard against the **mock** adapter, so you can see the
comparison mechanics — identical task set, identical auth boundary,
identical grading, unmeasured fields reported as ``None`` — without an
account on anything. Then it shows the three real adapters answering the
pure half of the interface offline, checks that the three infrastructure
overlays enforce the same approval threshold, and prints the exit cost.

Exits non-zero if the portable core reaches past the four methods, if the
overlays disagree about the threshold, if a real adapter's session store
does anything other than fail with a named install command, or if the
scorecard reports a cold start nobody measured.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import iac
import scorecard
from adapters.aws import AgentCore
from adapters.azure import FoundryAgents
from adapters.base import ADAPTER_METHODS, PORTABLE, CloudUnavailable
from adapters.gcp import AgentPlatform
from adapters.mock import MockCloud
from portable import APPROVAL_THRESHOLD_CENTS, INBOUND
from tasks import TASKS

K = 5


def real_adapters() -> list[object]:
    """The three, configured the way a decision record would pin them."""
    return [
        AgentCore(region="us-east-1"),
        AgentPlatform(region="us-central1", use_agent_identity=True),
        FoundryAgents(region="eastus", hosted=True),
    ]


def run_scorecard(failures: list[str]) -> None:
    """The mechanics, against a mock adapter, at k repetitions."""
    print("\n=== the scorecard, against a mock adapter ===")
    print(f"  task set : {', '.join(t.name for t in TASKS)}")
    print(f"  k        : {K}")
    print(f"  gate     : {APPROVAL_THRESHOLD_CENTS} cents, on every "
          f"platform")

    entry, attempts = scorecard.score(MockCloud(), k=K)
    print()
    print(scorecard.render([entry]))
    print()
    print(f"  attempts : {len(attempts)}")
    print(f"  verified : {sum(1 for a in attempts if a.verified)}")
    failed = [a for a in attempts if not a.verified]
    print(f"  failures : {[a.task for a in failed] or 'none'}")

    if entry.cold_start_ms is not None:
        failures.append(
            "the mock adapter reported a cold start nobody measured"
        )
    if not entry.non_negotiables_met:
        failures.append("the approval gate did not hold on the mock adapter")
    if entry.verified_success_rate < 1.0:
        failures.append(
            f"the mock adapter verified "
            f"{entry.verified_success_rate:.0%} of attempts"
        )


def undefined_cost(failures: list[str]) -> None:
    """A cost per success with no successes under it is not a number."""
    print("\n=== a platform that verifies nothing ===")
    broken = MockCloud()
    # One task, graded so that nothing can pass: the point is the reported
    # field, not the fixture.
    from northstar_evals import StateGrader

    from tasks import Task

    impossible = Task(
        name="cannot_pass",
        goal="Customer reports a cracked lamp shade.",
        script=TASKS[0].script,
        grader=StateGrader().check(
            "impossible", lambda w: False, "this check never passes"
        ),
    )
    entry, _ = scorecard.score(broken, (impossible,), k=2)
    print(f"  verified_success_rate      : {entry.verified_success_rate}")
    print(f"  cents_per_verified_success : "
          f"{entry.cents_per_verified_success} "
          f"({scorecard.UNDEFINED_COST} means undefined, not free)")
    for reason in scorecard.compare([entry]):
        print(f"  rejected: {reason}")

    if entry.cents_per_verified_success != scorecard.UNDEFINED_COST:
        failures.append(
            "cost per success was a number with no successes under it"
        )


def adapters_offline(failures: list[str]) -> None:
    """The pure half of the interface works with no account."""
    print("\n=== the three adapters, offline ===")
    for adapter in real_adapters():
        principal = adapter.principal_for(INBOUND)
        print(f"\n  {adapter.name}")
        print(f"    tool_endpoint : {adapter.tool_endpoint()}")
        print(f"    exporter      : {adapter.exporter()}")
        print(f"    principal     : user={principal.user_id} "
              f"agent={principal.agent_id} "
              f"scopes={sorted(principal.scopes)}")
        print(f"    preview deps  : {adapter.preview_dependencies()}")
        print(f"    cold start    : {adapter.cold_start_ms()} "
              f"(unmeasured, never a vendor figure)")
        try:
            adapter.session_store()
        except CloudUnavailable as exc:
            print(f"    session_store : refused, and says how — "
                  f"{str(exc).splitlines()[0][:56]}...")
        else:
            failures.append(
                f"{adapter.name}.session_store() worked with no account"
            )
        if principal.user_id != "CUST-8841":
            failures.append(
                f"{adapter.name} mapped the user to {principal.user_id!r}"
            )
        if principal.agent_id == principal.user_id:
            failures.append(
                f"{adapter.name} collapsed the user and the agent"
            )


def publish_gap(failures: list[str]) -> None:
    """Development permissions do not survive publishing."""
    print("\n=== the permission migration teams find in production ===")
    azure = FoundryAgents(region="eastus")
    lost = azure.published_identity_gap(INBOUND)
    print(f"  roles the developer held that the published agent does not: "
          f"{lost}")
    print("  that is the identity boundary working, not a bug")

    if lost != ["admin:all"]:
        failures.append(
            f"the published-identity gap reported {lost}, want ['admin:all']"
        )


def overlays(failures: list[str]) -> None:
    """Three overlays, one threshold, validated by parsing."""
    print("\n=== the infrastructure overlays ===")
    found = iac.thresholds()
    for cloud in iac.OVERLAYS:
        overlay = iac.load_overlay(cloud)
        print(f"  {cloud:<6} {len(overlay.resources)} resources, "
              f"threshold={found[cloud]}, "
              f"outputs={sorted(overlay.outputs)}, "
              f"unused vars={overlay.unused_variables() or 'none'}")
    print(f"  all three enforce the same threshold: "
          f"{len(set(found.values())) == 1}")

    if set(found.values()) != {APPROVAL_THRESHOLD_CENTS}:
        failures.append(f"the overlays disagree about the threshold: {found}")
    for cloud in iac.OVERLAYS:
        overlay = iac.load_overlay(cloud)
        if "tool_endpoint" not in overlay.outputs:
            failures.append(f"{cloud} exports no tool endpoint")
        if overlay.unused_variables():
            failures.append(
                f"{cloud} declares unused variables: "
                f"{overlay.unused_variables()}"
            )


def portability(failures: list[str]) -> None:
    """What travels, what does not, and how wide the interface is."""
    print("\n=== portability and exit cost ===")
    print(f"  the core calls exactly {len(ADAPTER_METHODS)} methods: "
          f"{', '.join(ADAPTER_METHODS)}")
    print("  travels unchanged:")
    for item in PORTABLE:
        print(f"    - {item}")
    for adapter in real_adapters():
        cost = adapter.exit_cost()
        print(f"\n  {cost.cloud}: {len(cost.rebuilt)} things to rebuild, "
              f"{cost.preview_dependencies} preview dependencies")
        for item in cost.rebuilt:
            print(f"    - {item}")

    if len(ADAPTER_METHODS) != 4:
        failures.append(
            f"the adapter interface has {len(ADAPTER_METHODS)} methods, not 4"
        )


def main() -> int:
    print("Chapter 22 — one agent, three clouds, one scorecard")

    failures: list[str] = []
    run_scorecard(failures)
    undefined_cost(failures)
    adapters_offline(failures)
    publish_gap(failures)
    overlays(failures)
    portability(failures)

    print("\n--- what this proves ---")
    print("The portable core is genuinely portable: one agent, three")
    print("adapters, no changes to the loop, the tools, the policy, or the")
    print("graders. And a cross-cloud comparison is only meaningful when")
    print("the workload, the auth boundary, and the success definition are")
    print("held identical, with unmeasured values reported as unmeasured.")
    print("\nThe overlays are validated by parsing, not by applying. Each")
    print("one's header states exactly what it creates and what it costs")
    print("while it exists.")

    if failures:
        print("\nFAILED:")
        for line in failures:
            print(f"  - {line}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
