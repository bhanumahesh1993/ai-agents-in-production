"""The custom resource, the controller, and the edge object, as assertions.

The portability claim is the one worth protecting, and
``test_both_deployment_shapes_leave_the_same_world`` is where it lives.
Everything else guards the admission checks that keep an agent from
reaching a cluster without a policy reference or with its egress widened.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import copy

import pytest

from northstar_contracts import World
from northstar_runtime import MemoryCheckpointer

import manifests
from agent_builder import (
    APPROVAL_THRESHOLD_CENTS,
    ORDER,
    REFUND_CENTS,
    build_support_agent,
)
from controller import ADMISSION_LABEL, AgentController
from edge.session import SupportSession, hibernate_and_wake
from edge.storage import LocalStore, StorageCheckpointer
from manifests import ManifestError


def a_document() -> dict:
    """The shipped ``Agent`` resource, freshly parsed."""
    return manifests.load("agent.yaml")


# ------------------------------------------------------------- the resource


def test_the_agent_resource_carries_everything_an_incident_needs() -> None:
    """Version, pinned snapshot, budget, tools, policy, and egress."""
    spec = manifests.AgentSpec.from_manifest(a_document())
    assert spec.version == "v9"
    assert spec.snapshot and "latest" not in spec.snapshot
    assert spec.budget == {"cents": 120, "turns": 12}
    assert spec.tool_names == ["orders", "refunds"]
    assert spec.mcp_servers() == ["orders-mcp", "refunds-mcp"]
    assert spec.policy_ref == "support-agent-policy"
    assert spec.egress == "deny-by-default"


def test_the_refund_tool_declares_the_approval_rule() -> None:
    """The gate is a property of the resource, not of the prompt."""
    spec = manifests.AgentSpec.from_manifest(a_document())
    assert spec.approval_rule("refunds") == (
        f"aboveCents({APPROVAL_THRESHOLD_CENTS})"
    )
    assert spec.approval_rule("orders") is None


def test_admission_refuses_a_widened_egress() -> None:
    """An agent fetches URLs a customer supplied. There is one right value."""
    document = a_document()
    document["spec"]["egress"] = "allow-all"
    with pytest.raises(ManifestError):
        AgentController().admit(document)


def test_admission_refuses_a_floating_model_snapshot() -> None:
    """A quantization or snapshot change is a behaviour change."""
    document = a_document()
    document["spec"]["model"] = {"provider": "bedrock", "snapshot": "latest"}
    assert manifests.admission_problems(document)


def test_admission_refuses_a_missing_policy_reference() -> None:
    """Otherwise authorization is whatever the namespace default happens to be."""
    for field in manifests.REQUIRED_SPEC_FIELDS:
        document = a_document()
        del document["spec"][field]
        assert manifests.admission_problems(document), field


def test_admission_refuses_a_tool_without_a_server_reference() -> None:
    """Tools attach declaratively, not as inline definitions."""
    document = a_document()
    document["spec"]["tools"] = [{"name": "refunds"}]
    assert manifests.admission_problems(document)


def test_every_shipped_manifest_parses() -> None:
    """Validated by parsing, which is weaker than applying and not token."""
    loaded = manifests.load_all()
    assert set(loaded) == {
        "agent.yaml",
        "crd.yaml",
        "kind.yaml",
        "networkpolicy.yaml",
    }
    assert loaded["crd.yaml"]["spec"]["names"]["kind"] == "Agent"
    assert len(loaded["kind.yaml"]["nodes"]) == 4


def test_the_crd_requires_the_same_fields_admission_does() -> None:
    """A schema and a controller that disagree is a controller nobody trusts."""
    crd = manifests.load("crd.yaml")
    required = crd["spec"]["versions"][0]["required"]
    assert set(required) == set(manifests.REQUIRED_SPEC_FIELDS)


def test_the_kind_config_separates_the_three_pools() -> None:
    """Agents, sandboxes, and inference do not share a scaling signal."""
    pools = {
        (node.get("labels") or {}).get("northstar.dev/pool")
        for node in manifests.load("kind.yaml")["nodes"]
    }
    assert {"agents", "sandbox", "inference"} <= pools


# ------------------------------------------------------------ the controller


def test_reconciling_writes_four_things_and_a_status() -> None:
    controller = AgentController()
    status = controller.reconcile(a_document())

    assert controller.get("Deployment", "support-agent-worker")
    assert controller.get("NetworkPolicy", "support-agent-egress")
    assert controller.get("ConfigMap", "support-agent-config")
    assert status.running_version == "v9"
    assert status.egress == "deny-by-default"


def test_reconciling_is_idempotent() -> None:
    """What makes a controller safe to run in a loop."""
    controller = AgentController()
    first = controller.reconcile(a_document())
    second = controller.reconcile(a_document())
    assert first.config_hash == second.config_hash
    assert second.observed_generation > first.observed_generation


def test_the_config_hash_moves_when_behaviour_moves() -> None:
    """And only then. It is the effective configuration, as a resource."""
    controller = AgentController()
    base = controller.reconcile(a_document())

    bumped = a_document()
    bumped["spec"]["model"]["snapshot"] = "anthropic.other-snapshot-v2"
    assert controller.reconcile(bumped).config_hash != base.config_hash

    cosmetic = a_document()
    cosmetic["metadata"]["labels"] = {"team": "support"}
    assert controller.reconcile(cosmetic).config_hash == base.config_hash


def test_the_derived_network_policy_matches_the_workload() -> None:
    """A selector that drifts matches nothing and reports success."""
    controller = AgentController()
    controller.reconcile(a_document())
    policy = controller.get("NetworkPolicy", "support-agent-egress")
    deployment = controller.get("Deployment", "support-agent-worker")

    assert policy["spec"]["defaultDeny"] is True
    assert (
        policy["spec"]["podSelector"]["matchLabels"]
        == deployment["metadata"]["labels"]
        == {ADMISSION_LABEL: "support-agent"}
    )


def test_the_allowlist_is_exactly_what_the_resource_references() -> None:
    controller = AgentController()
    controller.reconcile(a_document())
    policy = controller.get("NetworkPolicy", "support-agent-egress")
    allowed = [
        rule["to"][0]["podSelector"]["matchLabels"]["app"]
        for rule in policy["spec"]["egress"]
    ]
    assert allowed == ["orders-mcp", "refunds-mcp", "postgres",
                       "otel-collector"]


def test_the_worker_never_autoscales_on_cpu() -> None:
    """A worker holding forty sessions through a human wait is idle by CPU."""
    controller = AgentController()
    controller.reconcile(a_document())
    deployment = controller.get("Deployment", "support-agent-worker")
    metrics = deployment["spec"]["autoscaling"]["metrics"]
    assert "cpu" not in metrics
    assert set(metrics) == {"queue_age_seconds", "active_sessions"}
    assert deployment["spec"]["podDisruptionBudget"]["minAvailable"] >= 1
    assert deployment["spec"]["terminationGracePeriodSeconds"] >= 600


def test_an_in_flight_run_finishes_on_the_version_it_started_on() -> None:
    """The failure mode the artifact reproduces rather than hides."""
    controller = AgentController()
    document = a_document()
    controller.reconcile(document)
    controller.start_run("support-agent", "in-flight")

    bumped = copy.deepcopy(document)
    bumped["spec"]["version"] = "v10"
    controller.reconcile(bumped)
    controller.start_run("support-agent", "after-edit")

    assert controller.version_for("in-flight") == "v9"
    assert controller.version_for("after-edit") == "v10"
    assert controller.draining() == ["after-edit", "in-flight"]
    controller.finish_run("in-flight")
    assert controller.draining() == ["after-edit"]


def test_a_run_cannot_be_admitted_against_an_unknown_agent() -> None:
    """No version, no budget, no policy. Fail closed."""
    with pytest.raises(KeyError):
        AgentController().start_run("nobody", "run-1")


def test_the_inventory_is_a_query_against_the_api_server() -> None:
    """Registry, inventory, and revocation come free when agents are objects."""
    controller = AgentController()
    controller.reconcile(a_document())
    inventory = controller.inventory()
    assert [row["name"] for row in inventory] == ["support-agent"]
    assert inventory[0]["egress"] == "deny-by-default"


# --------------------------------------------------------------- the edge


def test_the_session_survives_hibernation_without_repeating_the_refund() -> None:
    """The property the whole edge model is bought for."""
    storage = LocalStore("edge-1")
    world = World()
    try:
        session, snapshots = hibernate_and_wake("edge-1", storage, world)
        assert snapshots[0]["status"] == "running"
        assert snapshots[0]["refund_rows"] == 1
        assert snapshots[-1]["status"] == "succeeded"
        assert snapshots[-1]["refund_rows"] == 1
        assert world.total_refunded_cents(ORDER) == REFUND_CENTS
        assert session.wakes >= 2
    finally:
        storage.close()


def test_hibernating_costs_nothing_and_writes_nothing_new() -> None:
    """State is already durable. That is the point."""
    storage = LocalStore("edge-2")
    try:
        session = SupportSession("edge-2", storage)
        session.on_message("Customer reports a cracked lamp shade.")
        before = storage.writes
        session.on_hibernate()
        assert storage.writes == before
        assert session.hibernations == 1
    finally:
        storage.close()


def test_two_sessions_never_share_a_store() -> None:
    """The isolation boundary, obtained structurally rather than configured."""
    first = LocalStore("edge-a")
    second = LocalStore("edge-b")
    try:
        first.put("state", {"step": 1})
        assert second.get("state") is None
        assert first.session_id != second.session_id
    finally:
        first.close()
        second.close()


def test_the_storage_adapter_round_trips_a_run() -> None:
    """Twelve lines, and the entire storage-specific surface."""
    storage = LocalStore("edge-3")
    try:
        checkpointer = StorageCheckpointer(storage)
        assert checkpointer.load("nope") is None

        world = World()
        loop = build_support_agent(
            world, checkpointer=checkpointer, run_id="edge-3"
        )
        state = loop.run("Customer reports a cracked lamp shade.",
                         run_id="edge-3")
        restored = checkpointer.load("edge-3")
        assert restored is not None
        assert restored.status == state.status
        assert restored.step == state.step
    finally:
        storage.close()


# ------------------------------------------------------------ portability


def test_both_deployment_shapes_call_the_same_builder() -> None:
    """The portability claim the chapter makes and this artifact checks."""
    import edge.session as edge_session

    assert edge_session.build_support_agent is build_support_agent


def test_both_deployment_shapes_leave_the_same_world() -> None:
    """One agent, two schedulers, one ledger."""
    pod_world = World()
    pod_loop = build_support_agent(
        pod_world, checkpointer=MemoryCheckpointer(), run_id="run-pod"
    )
    pod_loop.run("Customer reports a cracked lamp shade.", run_id="run-pod")

    storage = LocalStore("run-edge")
    edge_world = World()
    try:
        hibernate_and_wake("run-edge", storage, edge_world)
    finally:
        storage.close()

    assert pod_world.snapshot() == edge_world.snapshot()
    assert pod_world.total_refunded_cents(ORDER) == REFUND_CENTS
    assert edge_world.total_refunded_cents(ORDER) == REFUND_CENTS


def test_the_builder_takes_its_limits_from_the_resource() -> None:
    """Budget and threshold are the resource's, not the code's."""
    spec = manifests.AgentSpec.from_manifest(a_document())
    world = World()
    loop = build_support_agent(
        world,
        checkpointer=MemoryCheckpointer(),
        run_id="run-limits",
        max_turns=int(spec.budget["turns"]),
        budget_cents=int(spec.budget["cents"]),
    )
    assert loop.budget.max_turns == 12
    assert loop.budget.max_cents == 120
