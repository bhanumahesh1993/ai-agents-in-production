"""A custom resource reconciled, and the same agent at the edge.

    python artifacts/ch23-k8s-edge/demo.py

Admits and reconciles the `Agent` resource, prints the four objects the
controller writes and the status it reports, reproduces the version-pinning
behaviour by editing the resource mid-run, then runs the same
`build_support_agent` inside a hibernating per-session edge object and
asserts that state survives and no side effect is repeated on resume.

Exits non-zero if admission lets a widened egress or a floating model
snapshot through, if reconciliation is not idempotent, if an in-flight run
moves to a version it did not start on, if the woken session repeats the
refund, or if the two deployment shapes leave different worlds.

**One honest deviation.** The chapter says `make kind-up && make
demo-ch23-k8s` brings up a local cluster. This demo starts no cluster and
runs no `kubectl`: no chapter demo in this repository may require a daemon,
and CI has neither Docker nor a kube context. The manifests ship as real
files validated by parsing and by the same admission checks the controller
applies, and the controller reconciles against an in-process API server.
`make kind-up` is the command that needs a cluster.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import copy
import json

import manifests
from agent_builder import ORDER, REFUND_CENTS, build_support_agent
from controller import ADMISSION_LABEL, AgentController
from edge.session import SupportSession, hibernate_and_wake
from edge.storage import LocalStore
from manifests import ManifestError
from northstar_contracts import World
from northstar_runtime import MemoryCheckpointer


def show(title: str, obj: object) -> None:
    """Print one reconciled object, indented."""
    print(f"\n-- {title} --")
    print(json.dumps(obj, indent=2, sort_keys=True, default=str))


def admission(failures: list[str]) -> dict:
    """The resource, and three edits a controller must refuse."""
    print("\n=== admission ===")
    document = manifests.load("agent.yaml")
    spec = manifests.AgentSpec.from_manifest(document)
    print(f"  name        : {spec.name}")
    print(f"  version     : {spec.version}")
    print(f"  snapshot    : {spec.snapshot}")
    print(f"  budget      : {spec.budget}")
    print(f"  tools       : {spec.tool_names} -> {spec.mcp_servers()}")
    print(f"  approval    : refunds {spec.approval_rule('refunds')}")
    print(f"  policyRef   : {spec.policy_ref}")
    print(f"  egress      : {spec.egress}")

    edits = {
        "widened egress": {"egress": "allow-all"},
        "floating snapshot": {
            "model": {"provider": "bedrock", "snapshot": "latest"}
        },
        "no policy reference": {"policyRef": ""},
    }
    for label, patch in edits.items():
        broken = copy.deepcopy(document)
        broken["spec"].update(patch)
        problems = manifests.admission_problems(broken)
        print(f"  refused: {label:<20} -> {problems[0][:58]}")
        if not problems:
            failures.append(f"admission accepted a {label}")
    return document


def reconcile(document: dict, failures: list[str]) -> AgentController:
    """One object in, four objects and a status out."""
    print("\n=== reconcile ===")
    controller = AgentController()
    status = controller.reconcile(document)

    show("Deployment", controller.get("Deployment", "support-agent-worker"))
    show("NetworkPolicy", controller.get("NetworkPolicy",
                                         "support-agent-egress"))
    show("ConfigMap", controller.get("ConfigMap", "support-agent-config"))
    show("status subresource", status.to_dict())

    print("\n-- kubectl get agents --")
    for row in controller.inventory():
        print(f"  {row['name']:<16} {row['version']:<4} "
              f"{row['configHash']}  egress={row['egress']}")

    again = controller.reconcile(document)
    print(f"\n  reconciling twice is idempotent: "
          f"{status.config_hash == again.config_hash}")

    policy = controller.get("NetworkPolicy", "support-agent-egress")
    allowed = [
        e["to"][0]["podSelector"]["matchLabels"]["app"]
        for e in policy["spec"]["egress"]
    ]
    print(f"  egress allowlist: {allowed}")

    if status.config_hash != again.config_hash:
        failures.append("reconciliation is not idempotent")
    if not policy["spec"]["defaultDeny"]:
        failures.append("the derived network policy does not default-deny")
    if policy["spec"]["podSelector"]["matchLabels"] != {
        ADMISSION_LABEL: "support-agent"
    }:
        failures.append("the policy selector does not match the workload")
    return controller


def version_pinning(
    controller: AgentController,
    document: dict,
    failures: list[str],
) -> None:
    """Edit the resource mid-run. The failure mode, reproduced."""
    print("\n=== version pinning across an edit ===")
    controller.start_run("support-agent", "run-in-flight")
    before = controller.version_for("run-in-flight")

    bumped = copy.deepcopy(document)
    bumped["spec"]["version"] = "v10"
    new_status = controller.reconcile(bumped)
    controller.start_run("support-agent", "run-after-edit")

    after_in_flight = controller.version_for("run-in-flight")
    after_new = controller.version_for("run-after-edit")
    print(f"  in-flight run started on : {before}")
    print(f"  resource now says        : {new_status.running_version}")
    print(f"  in-flight run still on   : {after_in_flight}")
    print(f"  the next run starts on   : {after_new}")
    print(f"  draining                 : {controller.draining()}")
    controller.finish_run("run-in-flight")
    controller.finish_run("run-after-edit")
    print(f"  after both finish        : {controller.draining() or 'empty'}")

    if after_in_flight != before:
        failures.append("an in-flight run moved to a version it did not start on")
    if after_new != "v10":
        failures.append(f"a new run started on {after_new}, not v10")


def kubernetes_path(failures: list[str]) -> World:
    """What the worker pod runs: the shared builder, a durable store."""
    print("\n=== the Kubernetes worker ===")
    world = World()
    loop = build_support_agent(
        world, checkpointer=MemoryCheckpointer(), run_id="run-k8s"
    )
    state = loop.run("Customer reports a cracked lamp shade.",
                     run_id="run-k8s")
    print(f"  status     : {state.status}")
    print(f"  ledger     : {len(world.refunds_for(ORDER))} row(s), "
          f"{world.total_refunded_cents(ORDER)} cents")
    print(f"  builder    : build_support_agent")

    if world.total_refunded_cents(ORDER) != REFUND_CENTS:
        failures.append("the worker path left the wrong ledger")
    return world


def edge_path(failures: list[str]) -> World:
    """The same agent, in a hibernating per-session object."""
    print("\n=== the edge session ===")
    storage = LocalStore("run-edge")
    world = World()
    try:
        session, snapshots = hibernate_and_wake("run-edge", storage, world)
        for index, snap in enumerate(snapshots):
            phase = "asleep after step" if index == 0 else "awake, resumed"
            print(f"  {phase:<20} step={snap['step']} "
                  f"status={snap['status']} "
                  f"refunds={snap['refund_rows']} "
                  f"cents={snap['refunded_cents']}")
        print(f"  storage keys        : {snapshots[-1]['storage_keys']}")
        print(f"  hibernation cost    : 0 (no worker, no connection, no "
              f"lease)")
        print(f"  builder             : build_support_agent")

        if snapshots[0]["refund_rows"] != 1:
            failures.append("the refund did not commit before hibernation")
        if snapshots[-1]["refund_rows"] != 1:
            failures.append(
                f"the woken session left "
                f"{snapshots[-1]['refund_rows']} refund rows"
            )
        if snapshots[-1]["status"] != "succeeded":
            failures.append(
                f"the woken session ended {snapshots[-1]['status']!r}"
            )
        if session.wakes < 2:
            failures.append("the session never woke")
    finally:
        storage.close()
    return world


def portability(k8s_world: World, edge_world: World,
                failures: list[str]) -> None:
    """Both paths import the same builder. Both leave the same world."""
    print("\n=== portability ===")
    left = k8s_world.snapshot()
    right = edge_world.snapshot()
    print(f"  worker world : refunds={left['refund_count']} "
          f"ledger={left['ledger_entries']}")
    print(f"  edge world   : refunds={right['refund_count']} "
          f"ledger={right['ledger_entries']}")
    print(f"  identical    : {left == right}")

    if left != right:
        failures.append(
            "the two deployment shapes left different worlds"
        )


def constraints() -> None:
    """What the edge model cannot do, stated before the rewrite."""
    print("\n=== edge constraints, which decide the fit ===")
    for line in (
        "per-invocation CPU and memory ceilings far below a pod's",
        "no general container runtime: an existing agent is a rewrite",
        "the model call still leaves the edge unless you use its own",
        "vendor concentration is the highest in the book: the",
        "  programming model *is* the platform",
    ):
        print(f"  - {line}")
    print("  the mitigation is this file's shape: decision logic in a plain")
    print("  module, platform code confined to the shell and the storage")
    print("  adapter, and the whole thing green against Chapter 21's stack.")


def main() -> int:
    print("Chapter 23 — a custom resource, and the same agent at the edge")

    failures: list[str] = []
    document = admission(failures)
    controller = reconcile(document, failures)
    version_pinning(controller, document, failures)
    k8s_world = kubernetes_path(failures)
    edge_world = edge_path(failures)
    portability(k8s_world, edge_world, failures)
    constraints()

    print("\n--- what this proves ---")
    print("An agent's decision logic is independent of its deployment")
    print("shape: the same builder runs under a Kubernetes controller and")
    print("inside a hibernating per-session edge object. The operational")
    print("differences are about scheduling, state locality, and idle")
    print("cost, not about the agent.")
    print("\nThe manifests are validated by parsing and by the controller's")
    print("own admission checks, not by applying: no demo here may require")
    print("a cluster. `make kind-up` is the command that needs one.")

    if failures:
        print("\nFAILED:")
        for line in failures:
            print(f"  - {line}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
