"""State-based tests. Nothing here asserts on a run status.

Both writers report ``succeeded``, both traces are green, and the budget
guard is never triggered. If a status could tell you what happened, this
chapter would not need to exist.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import orchestrator
import parallel_writers
import pytest
import subagent
from northstar_contracts import World
from orchestrator import ORDER_ID, REFUND_CENTS
from parallel_writers import BRIEF, WRITERS, conflicts, ledger_for
from subagent import FINDING_TOKEN_BUDGET, WriteToolInReader


@pytest.fixture
def world() -> World:
    """A world nobody has written to yet."""
    return World()


def test_a_reader_cannot_be_given_a_write_tool(world: World) -> None:
    """The isolation boundary fails at assembly, not at refund time."""
    with pytest.raises(WriteToolInReader) as caught:
        subagent.reader_registry(list(world.tools()))
    assert "issue_refund" in str(caught.value)

    # The read-only set assembles cleanly, and holds only reads.
    reads = subagent.reader_registry(subagent.read_bindings(world))
    assert set(reads.names()) == set(subagent.NORTHSTAR_READS)
    assert all(not spec.writes for spec in reads.specs())


def test_workers_leave_the_world_untouched(world: World) -> None:
    """Parallel reads are safe because nothing has to be undone."""
    before = world.snapshot()
    orchestrator.research(world)
    assert world.ledger == []
    assert world.snapshot() == before


def test_isolation_compresses_what_reaches_the_orchestrator(
    world: World,
) -> None:
    """The reason to isolate a worker is that its reads stay behind."""
    result = orchestrator.research(world)

    assert len(result.findings) == len(orchestrator.QUESTIONS)
    for finding in result.findings:
        assert finding.ok
        assert finding.tokens <= FINDING_TOKEN_BUDGET
        # Evidence crosses as references, never as copies.
        assert finding.evidence_refs
        assert all(r.startswith("artifact://") for r in finding.evidence_refs)

    assert result.intake_tokens < result.worker_tokens
    assert result.compression > 3.0


def test_a_finding_over_budget_is_cut_not_passed_through() -> None:
    """The return budget is enforced in the dispatch path, not requested."""
    from northstar_contracts import Message, RunState

    fat = RunState(
        run_id="run_fat",
        status="succeeded",
        messages=[
            Message(role="user", content="Which orders look wrong?"),
            Message(role="assistant", content="x " * 4000),
        ],
    )
    finding = subagent.compress(fat, max_tokens=FINDING_TOKEN_BUDGET)
    assert finding.tokens <= FINDING_TOKEN_BUDGET
    assert "truncated" in finding.claim


def test_two_writers_leave_an_incoherent_world(world: World) -> None:
    """Both correct, both idempotent, jointly indefensible."""
    for name in WRITERS:
        parallel_writers.run_writer(world, name)

    # One refund. This is not the Chapter 1 defect: nothing was paid twice.
    assert world.total_refunded_cents(ORDER_ID) == REFUND_CENTS
    assert len(world.refunds_for(ORDER_ID)) == 1

    # And the customer has been told a replacement is coming anyway.
    kinds = {e["kind"] for e in world.ledger}
    assert kinds == {"refund_issued", "message_sent", "escalated"}
    assert len(conflicts(world)) == 2


def test_both_writers_report_success_and_stay_in_budget(
    world: World,
) -> None:
    """Nothing in either run's own account of itself is wrong."""
    states = {
        name: parallel_writers.run_writer_state(world, name)
        for name in WRITERS
    }
    for name, state in states.items():
        assert state.status == "succeeded", name
        assert state.budget_spent_cents < 60, name
    assert conflicts(world)


def test_every_write_carried_a_derived_idempotency_key(
    world: World,
) -> None:
    """And it did not help, which is the point of the warning box.

    A key makes one intent safe to repeat. It has nothing to say about two
    different intents, each executed exactly once, that should never both
    have happened.
    """
    for name in WRITERS:
        parallel_writers.run_writer(world, name)

    refund = world.refunds_for(ORDER_ID)[0]
    assert refund.idempotency_key
    writes = [
        c for c in world.calls
        if c["tool"] in {"issue_refund", "send_message"}
    ]
    assert writes
    assert all(c["arguments"].get("idempotency_key") for c in writes)
    assert conflicts(world)


def test_the_ledger_attributes_each_effect_to_its_writer(
    world: World,
) -> None:
    """Attribution has to be recorded at the boundary; it is not free."""
    for name in WRITERS:
        parallel_writers.run_writer(world, name)

    a = [e["kind"] for e in ledger_for(world, "writer_a")]
    b = [e["kind"] for e in ledger_for(world, "writer_b")]
    assert a == ["refund_issued"]
    assert b == ["message_sent", "escalated"]


def test_the_orchestrator_shape_settles_the_brief_before_writing(
    world: World,
) -> None:
    """Same brief, same two resolutions, one decision, one coherent world."""
    result = orchestrator.resolve_ticket(world, BRIEF)

    # Both candidate resolutions were considered — as evidence.
    assert len(result.findings) == 2
    claims = " ".join(f.claim.lower() for f in result.findings)
    assert "refundable" in claims
    assert "in stock" in claims

    assert conflicts(world) == []
    assert world.total_refunded_cents(ORDER_ID) == REFUND_CENTS
    assert not any(e["kind"] == "escalated" for e in world.ledger)


def test_the_advisors_could_not_have_acted_on_their_findings(
    world: World,
) -> None:
    """The lead is the only identity in act three that holds a write scope."""
    orchestrator.resolve_ticket(world, BRIEF)
    reads = subagent.reader_registry(subagent.read_bindings(world))
    assert "issue_refund" not in reads
    assert "send_message" not in reads
    assert orchestrator.lead_principal().has("refunds:write")
