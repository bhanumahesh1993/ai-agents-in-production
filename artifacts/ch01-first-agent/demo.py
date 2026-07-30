"""Run the Chapter 1 incident, then its repair.

    python -m artifacts.ch01_first_agent.demo

Both runs end with status ``succeeded``. Only one of them leaves the world in
the state the customer was promised. Exits non-zero if that is not what
happens, so this doubles as a regression test on the fault injector.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import sys

import tools_broken
import tools_repaired
from loop import MinimalAgent
from northstar_contracts import ToolCall, World
from northstar_runtime import FakeModel

# A partial refund on the larger order. This matters: a full refund would hit
# the world's own over-refund guard, and the guard -- not the idempotency key --
# would be what stopped the duplicate. The incident has to be reproducible
# *without* that safety net for the repair to be the thing under test.
ORDER = "NR-2026-0041827"   # US$84.00, delivered, two items
SKU = "NR-LAMPSHADE-03"
AMOUNT = 3250               # cents. Always integer cents, never a float.
GOAL = "Customer says the lamp shade in this order arrived cracked."


def script() -> list[object]:
    """The trajectory the model takes: read, check policy, refund, reply.

    Scripted so the run is identical every time. The interesting variable in
    this demo is the tool, not the model.
    """
    return [
        ToolCall("c1", "get_order", {"order_id": ORDER}),
        ToolCall("c2", "get_policy", {"sku": SKU, "reason": "damaged"}),
        ToolCall("c3", "issue_refund", {"order_id": ORDER,
                                        "amount_cents": AMOUNT,
                                        "reason": "damaged"}),
        "Refunded in full and apologies for the damage.",
    ]


def read_tools(world: World) -> dict[str, object]:
    """The two read tools, taken straight from the world's own bindings."""
    wanted = {"get_order", "get_policy"}
    return {
        spec.name: (spec, fn)
        for spec, fn in world.tools()
        if spec.name in wanted
    }


def run_broken() -> tuple[World, MinimalAgent]:
    """No idempotency key. The refund service times out after committing."""
    world = World()
    world.inject_fault("issue_refund", kind="timeout")

    tools = read_tools(world)
    tools["issue_refund"] = (
        tools_broken.SPEC,
        tools_broken.make_issue_refund(world),
    )

    agent = MinimalAgent(FakeModel(default=script()), tools,
                         run_id="run_ch01_broken")
    agent.run(GOAL)
    return world, agent


def run_repaired() -> tuple[World, MinimalAgent]:
    """Same trajectory, same fault, key derived from (run_id, step)."""
    world = World()
    world.inject_fault("issue_refund", kind="timeout")

    tools = read_tools(world)
    agent_ref: dict[str, MinimalAgent] = {}
    tools["issue_refund"] = (
        tools_repaired.SPEC,
        tools_repaired.make_issue_refund(
            world,
            run_id="run_ch01_repaired",
            step_of=lambda: agent_ref["a"].state.step,
        ),
    )

    agent = MinimalAgent(FakeModel(default=script()), tools,
                         run_id="run_ch01_repaired")
    agent_ref["a"] = agent
    agent.run(GOAL)
    return world, agent


def report(label: str, world: World, agent: MinimalAgent) -> int:
    """Print the trajectory and the ledger, and return the refund count."""
    refunds = world.refunds_for(ORDER)
    total = world.total_refunded_cents(ORDER)
    print(f"\n=== {label} ===")
    print(f"run status      : {agent.state.status}")
    print(f"trajectory      : {' -> '.join(agent.trajectory())}")
    print(f"tool calls made : issue_refund x{world.call_count('issue_refund')}")
    print(f"ledger          : {len(refunds)} refund(s), {total} cents total")
    print(f"claim was       : {AMOUNT} cents")
    verdict = "MATCHES the claim" if total == AMOUNT else "DOES NOT MATCH"
    print(f"world vs claim  : {verdict}")
    return len(refunds)


def main() -> int:
    broken_world, broken_agent = run_broken()
    n_broken = report("broken: no idempotency key", broken_world, broken_agent)

    repaired_world, repaired_agent = run_repaired()
    n_repaired = report(
        "repaired: derived idempotency key", repaired_world, repaired_agent
    )

    print("\n--- what this proves ---")
    print("Both runs reported success and both traces look healthy.")
    print("Only the ledger distinguishes them, which is why graders read the")
    print("world and not the transcript.")

    failures: list[str] = []
    if n_broken != 2:
        failures.append(f"expected 2 refunds in the broken run, got {n_broken}")
    if n_repaired != 1:
        failures.append(
            f"expected 1 refund in the repaired run, got {n_repaired}"
        )
    if failures:
        print("\nFAILED:")
        for line in failures:
            print(f"  - {line}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
