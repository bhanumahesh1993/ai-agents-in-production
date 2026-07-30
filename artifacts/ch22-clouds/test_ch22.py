"""One agent, three adapters, one scorecard, as assertions.

The portability claim is checkable and this file checks it: the portable
core calls exactly four methods, every adapter satisfies the same protocol,
the three overlays enforce the same threshold, and the scorecard reports
what it measured rather than what a vendor page says.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import iac
import portable
import pytest
import scorecard
from adapters.aws import AgentCore
from adapters.azure import FoundryAgents
from adapters.base import (
    ADAPTER_METHODS,
    CloudAdapter,
    CloudUnavailable,
    extra_methods,
)
from adapters.gcp import AgentPlatform
from adapters.mock import MockCloud
from northstar_contracts import World
from northstar_evals import StateGrader
from northstar_policy import Principal
from northstar_runtime import Checkpointer
from portable import APPROVAL_THRESHOLD_CENTS, INBOUND
from tasks import TASKS, Task, task_named


def real_adapters() -> list[CloudAdapter]:
    """The three, configured as a decision record would pin them."""
    return [
        AgentCore(region="us-east-1"),
        AgentPlatform(region="us-central1", use_agent_identity=True),
        FoundryAgents(region="eastus", hosted=True),
    ]


# ------------------------------------------------------------ the interface


def test_the_interface_has_exactly_four_methods() -> None:
    """A fifth is a plane you meant to keep portable, reaching in."""
    assert ADAPTER_METHODS == (
        "session_store",
        "tool_endpoint",
        "principal_for",
        "exporter",
    )
    assert len(ADAPTER_METHODS) == 4


def test_every_adapter_satisfies_the_protocol() -> None:
    for adapter in [MockCloud(), *real_adapters()]:
        assert isinstance(adapter, CloudAdapter)
        for method in ADAPTER_METHODS:
            assert callable(getattr(adapter, method))


def test_the_portable_core_calls_only_the_four() -> None:
    """The check that actually protects the claim.

    An adapter may expose more — the scorecard asks for a cold start and a
    preview count — but the *core* must not reach for them, or the extra
    becomes load-bearing without anyone deciding that it should.
    """
    calls: list[str] = []

    class Recording:
        name = "recording"
        region = "nowhere"

        def __getattr__(self, item: str) -> object:
            calls.append(item)
            raise AttributeError(item)

        def session_store(self) -> Checkpointer:
            calls.append("session_store")
            return MockCloud().session_store()

        def tool_endpoint(self) -> str:
            calls.append("tool_endpoint")
            return "recording://tools"

        def principal_for(self, inbound: dict) -> Principal:
            calls.append("principal_for")
            return MockCloud().principal_for(inbound)

        def exporter(self) -> str:
            calls.append("exporter")
            return "memory"

    task = task_named("damaged_item_refund")
    portable.run_once(
        Recording(), task.name, task.goal, task.script, task.grader, "r-1"
    )
    assert set(calls) <= set(ADAPTER_METHODS)


def test_extra_methods_are_reported_rather_than_forbidden() -> None:
    """A number for the exit-cost note, not a failure."""
    assert "cold_start_ms" in extra_methods(MockCloud())
    assert "session_store" not in extra_methods(MockCloud())


# -------------------------------------------------------------- offline use


def test_no_adapter_reaches_a_cloud_at_import_or_on_the_pure_methods() -> None:
    """The whole file must be readable and runnable with no credentials."""
    for adapter in real_adapters():
        assert adapter.tool_endpoint().startswith("https://")
        assert adapter.exporter().startswith("otlp://")
        assert isinstance(adapter.principal_for(INBOUND), Principal)


def test_a_session_store_fails_with_the_install_command_named() -> None:
    """Naming the command beats a default that silently reaches a cloud."""
    for adapter in real_adapters():
        with pytest.raises(CloudUnavailable) as caught:
            adapter.session_store()
        assert "pip install" in str(caught.value)


def test_every_adapter_keeps_the_user_and_the_agent_apart() -> None:
    """Collapsing them here would undo Chapter 19 one layer below."""
    for adapter in [MockCloud(), *real_adapters()]:
        principal = adapter.principal_for(INBOUND)
        assert principal.user_id == "CUST-8841"
        assert principal.agent_id == "northstar-support-agent"
        assert principal.user_id != principal.agent_id
        assert "refunds:write" in principal.scopes


def test_publishing_does_not_carry_the_developer_permissions() -> None:
    """The failure teams meet in their first production week."""
    azure = FoundryAgents(region="eastus")
    assert azure.published_identity_gap(INBOUND) == ["admin:all"]
    assert "admin:all" not in azure.principal_for(INBOUND).scopes


def test_preview_dependencies_are_counted_not_assumed_away() -> None:
    """The count predicts unplanned work better than a feature matrix."""
    assert AgentPlatform(region="r", use_agent_identity=False)\
        .preview_dependencies() == 0
    assert AgentPlatform(region="r", use_agent_identity=True)\
        .preview_dependencies() == 1
    assert FoundryAgents(region="r", hosted=True).preview_dependencies() == 1


# --------------------------------------------------------------- the score


def test_the_scorecard_never_guesses_a_cold_start() -> None:
    entry, _ = scorecard.score(MockCloud(), k=2)
    assert entry.cold_start_ms is None


def test_cost_divides_by_graded_successes_not_by_invocations() -> None:
    """A platform that is cheap per call and fails more often is not cheap."""
    task = task_named("damaged_item_refund")
    good, _ = scorecard.score(MockCloud(), (task,), k=4)
    assert good.verified_success_rate == 1.0
    assert good.cents_per_verified_success >= 0

    impossible = Task(
        name="cannot_pass",
        goal=task.goal,
        script=task.script,
        grader=StateGrader().check("never", lambda w: False, "never passes"),
    )
    bad, _ = scorecard.score(MockCloud(), (impossible,), k=4)
    assert bad.verified_success_rate == 0.0
    assert bad.cents_per_verified_success == scorecard.UNDEFINED_COST


def test_a_platform_failing_a_non_negotiable_is_rejected() -> None:
    """Whatever its aggregate score."""
    gate = task_named("flagged_refund_needs_a_human")
    assert gate.non_negotiable is True
    entry, _ = scorecard.score(MockCloud(), (gate,), k=3)
    assert entry.non_negotiables_met is True
    assert scorecard.compare([entry]) == []


def test_the_policy_gate_is_enforced_on_every_run() -> None:
    """A benchmark without the gate is a benchmark of a different system."""
    gate = task_named("flagged_refund_needs_a_human")
    world = World()
    loop = portable.build_loop(MockCloud(), world, gate.script)
    state = loop.run(gate.goal, run_id="gate-1")
    assert state.status == "waiting_approval"
    assert world.ledger == []


def test_the_task_set_can_be_lost() -> None:
    """A fixture with one right answer measures nothing."""
    mug = task_named("damaged_mug_refund")
    world = World()
    # Refund the wrong order and the grader must catch it.
    world.issue_refund("NR-2026-0041827", 3250, "damaged",
                       idempotency_key="k")
    from northstar_contracts import RunState

    grade = mug.grader.grade(RunState(run_id="x"), world)
    assert grade.passed is False


def test_percentiles_come_from_measured_durations() -> None:
    assert scorecard.percentile([], 0.5) == 0
    assert scorecard.percentile([1, 2, 3, 4], 0.5) == 2
    assert scorecard.percentile([1, 2, 3, 4], 0.95) == 4


def test_the_score_reports_only_the_ten_fields() -> None:
    entry, _ = scorecard.score(MockCloud(), (TASKS[0],), k=1)
    assert set(entry.to_dict()) == {
        "cloud",
        "region",
        "verified_success_rate",
        "pass_k",
        "p50_ms",
        "p95_ms",
        "cold_start_ms",
        "cents_per_verified_success",
        "preview_dependencies",
        "non_negotiables_met",
    }


# ------------------------------------------------------------- the overlays


def test_the_three_overlays_enforce_the_same_threshold() -> None:
    """Different numbers means comparing three policies, not three clouds."""
    found = iac.thresholds()
    assert set(found.values()) == {APPROVAL_THRESHOLD_CENTS}


def test_every_overlay_parses_and_exports_a_tool_endpoint() -> None:
    for cloud in iac.OVERLAYS:
        overlay = iac.load_overlay(cloud)
        assert overlay.resources
        assert "tool_endpoint" in overlay.outputs
        assert overlay.unused_variables() == []


def test_every_overlay_pins_residency_as_a_required_variable() -> None:
    """Residency is a decision, not an inherited default."""
    for cloud in iac.OVERLAYS:
        overlay = iac.load_overlay(cloud)
        region = overlay.variables["region"]
        assert "default" not in region.body


def test_the_azure_overlay_declares_the_published_role_assignment() -> None:
    """The permission migration, as a reviewed change rather than a bug."""
    overlay = iac.load_overlay("azure")
    assert any(
        "role_assignment" in resource for resource in overlay.resources
    )


def test_the_hcl_parser_refuses_what_it_does_not_cover() -> None:
    """Not an HCL implementation, and it should not grow into one."""
    with pytest.raises(iac.HCLError):
        iac.load_overlay("no-such-cloud")
