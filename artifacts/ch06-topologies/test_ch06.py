"""What the topology changes, and what only the handoff changes.

Every assertion here reads either the authoritative store or a measurement
taken from the run. All three configurations report ``succeeded``, so a
status assertion would pass on the one that pays twice.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import pytest
import supervisor
import swarm
import topology
from compare import CONFIGURATIONS, TraceRow, compare
from handoff import (
    CONTRACT_CATEGORIES,
    BudgetExhausted,
    Handoff,
    NotPermitted,
    load_contract,
    refund_key,
    refund_key_local,
)
from northstar_contracts import RunState
from topology import ORDER_ID, REFUND_CENTS

HERE = Path(__file__).resolve().parent


@pytest.fixture(scope="module")
def rows() -> list[TraceRow]:
    """All three configurations, each against its own faulted world."""
    return compare()


def _by_name(rows: list[TraceRow]) -> dict[str, TraceRow]:
    return {r.name: r for r in rows}


def test_only_the_dropped_contract_pays_twice(rows: list[TraceRow]) -> None:
    """Same agents, same tools, same fault. One field's worth of difference."""
    by_name = _by_name(rows)

    for name in (CONFIGURATIONS[0], CONFIGURATIONS[1]):
        assert by_name[name].refund_rows == 1, name
        assert by_name[name].refunded_cents == REFUND_CENTS, name

    dropped = by_name[CONFIGURATIONS[2]]
    assert dropped.refund_rows == 2
    assert dropped.refunded_cents == 2 * REFUND_CENTS


def test_all_three_report_success(rows: list[TraceRow]) -> None:
    """Which is why the ledger is the thing under test and not the status."""
    assert {r.status for r in rows} == {"succeeded"}


def test_the_anchor_is_the_whole_difference(rows: list[TraceRow]) -> None:
    """One identity per intent, or one identity per attempt."""
    by_name = _by_name(rows)
    assert by_name[CONFIGURATIONS[1]].distinct_keys == 1
    assert by_name[CONFIGURATIONS[2]].distinct_keys == 2
    # Both presented the service two calls; only one presented two identities.
    assert len(by_name[CONFIGURATIONS[1]].keys_presented) == 2
    assert len(by_name[CONFIGURATIONS[2]].keys_presented) == 2


def test_refund_key_is_a_pure_function_of_the_origin() -> None:
    """Carried across the hop, it does not depend on who is executing."""
    contract = Handoff(
        origin_run_id="run_origin",
        origin_step_id=7,
        goal="assess",
        allowed_tools=("get_order",),
        prohibited_tools=("issue_refund",),
        approval_threshold_cents=5000,
        budget_cents_left=40,
        turns_left=5,
        return_to="support-orchestrator",
        deadline_ts=time.time() + 60,
    )
    assert refund_key(contract) == refund_key(contract)

    # Two hops later, with a different receiver, the key is unchanged.
    next_hop = contract.narrow(
        "specialist@1.0.0", "second look", ("get_order",), spend_turns=1
    )
    assert refund_key(next_hop) == refund_key(contract)

    # The local derivation changes with every step the receiver takes.
    first = refund_key_local(RunState(run_id="run_receiver", step=3))
    second = refund_key_local(RunState(run_id="run_receiver", step=4))
    assert first != second
    assert first != refund_key(contract)


def test_a_hop_can_only_narrow_permissions() -> None:
    """A receiver's effective permissions must be a subset of the sender's."""
    contract = Handoff(
        origin_run_id="run_origin",
        origin_step_id=7,
        goal="assess",
        allowed_tools=("get_order", "get_policy"),
        prohibited_tools=("issue_refund",),
        approval_threshold_cents=5000,
        budget_cents_left=40,
        turns_left=5,
        return_to="support-orchestrator",
        deadline_ts=time.time() + 60,
    )
    with pytest.raises(NotPermitted):
        contract.narrow("wider@1.0", "pay it", ("issue_refund",))

    narrowed = contract.narrow(
        "reader@1.0", "just read", ("get_order",), spend_cents=10, spend_turns=1
    )
    assert narrowed.allowed_tools == ("get_order",)
    assert narrowed.approval_threshold_cents == 5000
    with pytest.raises(NotPermitted):
        narrowed.require("issue_refund")


def test_budgets_are_remainders_not_fresh_allowances() -> None:
    """A chain that resets the counter spends a full budget at every hop."""
    contract = Handoff(
        origin_run_id="run_origin",
        origin_step_id=7,
        goal="assess",
        allowed_tools=("get_order",),
        prohibited_tools=(),
        approval_threshold_cents=5000,
        budget_cents_left=30,
        turns_left=2,
        return_to="support-orchestrator",
        deadline_ts=time.time() + 60,
    )
    hop = contract.narrow(
        "b@1", "next", ("get_order",), spend_cents=20, spend_turns=1
    )
    assert hop.budget_cents_left == 10
    assert hop.turns_left == 1
    assert hop.chain == ("fraud-review@1.8.0",)

    with pytest.raises(BudgetExhausted):
        hop.narrow("c@1", "next", ("get_order",), spend_cents=10)


def test_the_printed_contract_and_the_typed_one_agree() -> None:
    """``handoff.yaml`` and ``Handoff`` must not drift apart unnoticed."""
    printed = load_contract(HERE / "handoff.yaml")
    typed = set(Handoff.__dataclass_fields__)

    for category, fields in CONTRACT_CATEGORIES.items():
        assert set(fields) <= typed, category

    for required in (
        "origin_run_id",
        "origin_step_id",
        "approval_threshold_cents",
        "trace_parent",
        "auth_context_ref",
        "return_to",
        "on_timeout",
    ):
        assert required in printed, required

    # One thing must never move: a raw credential.
    assert printed["auth_context_ref"].startswith("delegation://")


def test_workers_in_the_supervisor_topology_cannot_write(
    rows: list[TraceRow],
) -> None:
    """Every write stays in the supervisor's loop, by construction."""
    world = topology.build_world()
    registry = supervisor._worker_registry(world)
    assert set(registry.names()) <= set(supervisor.WORKER_TOOLS)
    assert all(not spec.writes for spec in registry.specs())
    assert supervisor.DELEGATE.writes is False
    assert supervisor.DELEGATE.max_result_tokens == 400


def test_the_swarm_finishes_in_fewer_turns(rows: list[TraceRow]) -> None:
    """No subtask makes a round trip through a coordinator."""
    by_name = _by_name(rows)
    assert by_name[CONFIGURATIONS[1]].turns < by_name[CONFIGURATIONS[0]].turns
    # And each of its turns costs more, because the transcript travels.
    swarm_row = by_name[CONFIGURATIONS[1]]
    sup_row = by_name[CONFIGURATIONS[0]]
    assert (
        swarm_row.tokens / swarm_row.turns
        > sup_row.tokens / sup_row.turns
    )


def test_every_configuration_named_an_owner_at_the_timeout(
    rows: list[TraceRow],
) -> None:
    """When the call times out, exactly one component must be pointable-at."""
    for row in rows:
        assert row.owner_step >= 0, row.name
        assert row.owner in {
            "supervisor",
            "fraud-review",
        }, f"{row.name}: {row.owner}"

    by_name = _by_name(rows)
    assert by_name[CONFIGURATIONS[0]].owner == "supervisor"
    assert by_name[CONFIGURATIONS[1]].owner == "fraud-review"
    # The third has an executing component and no anchored responsibility.
    assert by_name[CONFIGURATIONS[1]].anchored is True
    assert by_name[CONFIGURATIONS[2]].anchored is False


def test_the_over_threshold_refund_asked_a_human(
    rows: list[TraceRow],
) -> None:
    """12,000 cents on a fraud-flagged order is not an autonomous decision."""
    for row in rows:
        assert row.approvals >= 1, row.name


def test_the_transfer_carries_no_raw_credential() -> None:
    """The authorisation that travels is a reference to a delegation."""
    world = topology.build_world()
    run = swarm.run_swarm(world, carry_contract=True)
    assert run.handoff is not None
    payload = run.handoff.to_dict()
    assert payload["auth_context_ref"].startswith("delegation://")
    assert payload["return_to"]
    assert payload["on_timeout"]
    assert payload["evidence_refs"] == [f"artifact://orders/{ORDER_ID}"]
    # A reference, not a copy of the order.
    assert all("://" in ref for ref in payload["evidence_refs"])
