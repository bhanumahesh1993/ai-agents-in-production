"""One orchestrator, three readers, and two writers who disagree.

    python artifacts/ch05-orchestrator/demo.py
    python artifacts/ch05-orchestrator/demo.py --assert-coherent

Three acts. The orchestrator fans out three isolated read-only workers and
prints the context-compression ratio. The parallel-writer demo gives two
writers one open brief and full tool access, and both finish green. Then the
same brief goes through the orchestrator shape, where the two candidate
resolutions come back as findings and the lead picks one before anything is
written.

The default exit code is 0 when every one of those behaves as designed,
including the deliberately broken middle act, so ``make demos`` stays a
smoke test. Pass ``--assert-coherent`` to assert on the world instead: it
exits non-zero because the ledger holds two conflicting resolutions for
order NR-2026-0041827, which is the failure the chapter is about.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import orchestrator
import parallel_writers
import subagent
from northstar_contracts import World
from orchestrator import ORDER_ID
from parallel_writers import BRIEF, WRITERS, conflicts, ledger_for


def act_one() -> tuple[orchestrator.Research, list[str]]:
    """Three isolated readers, one reconciler, and the compression ratio."""
    world = World()
    result = orchestrator.research(world)

    print("=== act 1: orchestrator with three read-only workers ===")
    for finding in result.findings:
        print(f"\n  question : {finding.question}")
        print(f"  tools    : {' -> '.join(finding.tool_calls)}")
        print(f"  worker   : {finding.worker_tokens} tokens held")
        print(f"  returned : {finding.tokens} tokens")
        print(f"  claim    : {finding.claim[:72]}...")

    print(f"\n  workers held      : {result.worker_tokens} tokens")
    print(f"  crossed inbound   : {result.intake_tokens} tokens")
    print(f"  compression ratio : {result.compression:.1f}x")
    print(f"  lead context      : {result.lead_tokens} tokens")
    print(f"  lead status       : {result.state.status}")
    print(f"  side effects      : {len(world.ledger)}")

    failures: list[str] = []
    for finding in result.findings:
        if finding.tokens > subagent.FINDING_TOKEN_BUDGET:
            failures.append(
                f"finding for {finding.question!r} is {finding.tokens} "
                f"tokens, over the {subagent.FINDING_TOKEN_BUDGET} budget"
            )
        if not finding.ok:
            failures.append(f"worker failed: {finding.question!r}")
    if result.compression <= 1.0:
        failures.append(
            f"compression ratio {result.compression:.2f}x: the boundary "
            f"bought nothing"
        )
    if world.ledger:
        failures.append(
            f"the research run wrote {len(world.ledger)} side effect(s); "
            f"read-only workers and a reconciling lead should write none"
        )

    # The boundary is a registry, not an instruction. Prove it.
    try:
        subagent.reader_registry(list(World().tools()))
    except subagent.WriteToolInReader as exc:
        print(f"\n  write tool offered to a reader -> {type(exc).__name__}")
    else:
        failures.append("a read-only worker accepted a write tool")

    return result, failures


def act_two() -> tuple[World, list[str]]:
    """Two green, budget-compliant, individually correct writer runs."""
    world = World()
    print("\n=== act 2: two writers, one brief, full tool access ===")
    print(f"  brief: {BRIEF}")

    for name in WRITERS:
        state = parallel_writers.run_writer_state(world, name)
        effects = ledger_for(world, name)
        print(f"\n  {name}")
        print(f"    status  : {state.status}")
        print(f"    effects : {[e['kind'] for e in effects]}")
        for effect in effects:
            detail = effect.get("body") or effect.get("reason") or ""
            print(f"      - {effect['kind']}: {str(detail)[:56]}")

    found = conflicts(world)
    print("\n  conflicting resolutions in the ledger:")
    for line in found or ["(none)"]:
        print(f"    - {line}")

    failures: list[str] = []
    if not found:
        failures.append(
            "the parallel-writer demo did not reproduce the conflict; "
            "the whole point of act 2 is that it does"
        )
    if world.total_refunded_cents(ORDER_ID) != orchestrator.REFUND_CENTS:
        failures.append(
            "act 2 should leave exactly one refund: the defect here is two "
            "different intents, not one intent paid twice"
        )
    return world, failures


def act_three() -> tuple[World, list[str]]:
    """The same brief, with the open decision settled before any write."""
    world = World()
    print("\n=== act 3: the same brief through the orchestrator shape ===")
    result = orchestrator.resolve_ticket(world, BRIEF)

    for finding in result.findings:
        print(f"\n  advisory : {finding.question}")
        print(f"  claim    : {finding.claim}")
    print(f"\n  lead wrote : {[e['kind'] for e in world.ledger]}")
    found = conflicts(world)
    print(f"  conflicts  : {found or '(none)'}")

    failures: list[str] = []
    if found:
        failures.append(
            f"the orchestrated resolution still conflicts: {found}"
        )
    if world.total_refunded_cents(ORDER_ID) != orchestrator.REFUND_CENTS:
        failures.append("the orchestrated resolution did not refund once")
    if any(e["kind"] == "escalated" for e in world.ledger):
        failures.append(
            "the lead both refunded and queued a replacement dispatch"
        )
    return world, failures


def main(argv: list[str]) -> int:
    strict = "--assert-coherent" in argv

    _, f1 = act_one()
    broken_world, f2 = act_two()
    _, f3 = act_three()

    print("\n--- what this proves ---")
    print("Context isolation is enforceable in code at the tool-registry")
    print("boundary. Isolated read-only workers cut what reaches the")
    print("orchestrator by the ratio printed in act 1, without losing the")
    print("evidence, because references cross and transcripts do not.")
    print("And two independently correct writers produce an incoherent")
    print("world that no downstream merge, retry, or idempotency key can")
    print("repair, while both runs report success.")

    failures = f1 + f2 + f3
    if failures:
        print("\nFAILED:")
        for line in failures:
            print(f"  - {line}")
        return 1

    if strict:
        found = conflicts(broken_world)
        print("\n--assert-coherent: asserting on the world, not the runs")
        for line in found:
            print(f"  - {line}")
        if found:
            print("\nFAILED: the ledger holds conflicting resolutions.")
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
