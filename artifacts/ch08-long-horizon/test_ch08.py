"""The long-horizon run's properties, as assertions on stored state.

Every phase below opens its own stores and closes them, so what one phase
asserts about the next is only ever what a real second worker would have: two
SQLite files. Nothing is passed between phases in memory, because the whole
argument of the chapter is about what survives when memory does not.

The refund count is read from the refund service -- the store the workflow
does *not* own -- and never from the run's own account of itself. A run that
reports ``succeeded`` while the provider holds two settlements is the failure
this chapter removes, and a test that grades the checkpoint cannot see it.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import wiring
from envelope import ConfigDrift, Envelope, config_hash
from keys import derived_key, generated_key, key_for
from migrate import V7, V8, NoMigrationPath, plan
from migrate import migrate as run_migration
from northstar_contracts import (
    REFUND_APPROVAL_THRESHOLD_CENTS,
    RunState,
    ToolCall,
    World,
    idempotency_key,
)
from northstar_runtime import SimulatedCrash
from pause import AMOUNT_CENTS, APPROVAL_QUEUE, ORDER, RUN_ID, STEP_REFUND
from resume import RESUME_CHECKS, argument_diff, fingerprint_of, resume, with_key
from states import (
    TERMINAL,
    IllegalTransition,
    RunPhase,
    check_transition,
    holds_compute,
)

APPROVER = "rota:fraud-review"


def worker(
    opened: list[wiring.Wiring],
    state_dir: Path,
    *,
    version: str = V7,
    strategy: str = "derived",
    kill: bool = False,
) -> wiring.Wiring:
    """Open one worker's view of the two files, closed at teardown."""
    wired = wiring.build(
        state_dir,
        agent_version=version,
        key_strategy=strategy,
        kill_after_settle=kill,
    )
    opened.append(wired)
    return wired


def park(
    opened: list[wiring.Wiring],
    state_dir: Path,
    *,
    strategy: str = "derived",
) -> None:
    """Phase one: reach the refund and park in front of it, then exit."""
    wired = worker(opened, state_dir, strategy=strategy)
    wired.workflow.start()
    wired.close()


def decide(
    opened: list[wiring.Wiring],
    state_dir: Path,
    *,
    approved: bool = True,
) -> str:
    """Phase two: answer the newest request on the queue, then exit.

    Returns:
        The fingerprint the decision was bound to.
    """
    wired = worker(opened, state_dir)
    inbox = wired.approvals.inbox(APPROVAL_QUEUE)
    assert inbox, "nothing waiting on the fraud-review queue"
    decided = wired.approvals.decide(inbox[0].id, approved, APPROVER)
    wired.close()
    return decided.fingerprint


# ------------------------------------------------------- the pause returns


def test_the_pause_parks_the_run_and_holds_no_compute(
    opened: list[wiring.Wiring], state_dir: Path
) -> None:
    """Nothing raised, nothing half-done, and no lease held."""
    wired = worker(opened, state_dir)
    envelope = wired.workflow.start()

    assert envelope.phase is RunPhase.WAITING_APPROVAL
    assert holds_compute(envelope.phase) is False
    assert wired.service.total_cents(ORDER) == 0


def test_the_checkpoint_is_written_before_the_pause_returns(
    opened: list[wiring.Wiring], state_dir: Path
) -> None:
    """A crash between the decision and the return would lose nothing."""
    park(opened, state_dir)

    reader = worker(opened, state_dir)
    envelope = reader.store.load_envelope(RUN_ID)
    assert envelope.phase is RunPhase.WAITING_APPROVAL
    assert envelope.agent_version == V7
    assert envelope.schema_version == "1"
    assert envelope.pending_call is not None
    assert envelope.pending_call["name"] == "issue_refund"
    assert envelope.state.step == STEP_REFUND
    # The pre-flight check a deploy needs before it expires an old version.
    assert reader.store.parked() == [RUN_ID]


def test_the_request_goes_to_a_queue_not_a_person(
    opened: list[wiring.Wiring], state_dir: Path
) -> None:
    """Sixty-one hours was a routing problem, and routing is fixable."""
    park(opened, state_dir)
    reader = worker(opened, state_dir)
    request = reader.approvals.pending(RUN_ID)

    assert request is not None
    assert request.queue == APPROVAL_QUEUE
    assert request.decided is False
    assert request.arguments["amount_cents"] == AMOUNT_CENTS


# ------------------------------------------------------- the derived key


def test_a_derived_key_is_stable_and_a_generated_one_is_not() -> None:
    """The few characters of difference the whole chapter turns on."""
    assert derived_key(RUN_ID, STEP_REFUND) == derived_key(RUN_ID, STEP_REFUND)
    assert derived_key(RUN_ID, STEP_REFUND) == idempotency_key(
        RUN_ID, STEP_REFUND
    )
    assert generated_key() != generated_key()
    with pytest.raises(ValueError, match="unknown key strategy"):
        key_for(RUN_ID, STEP_REFUND, "probably-fine")


def test_a_resumed_worker_recomputes_the_key_without_storing_it(
    opened: list[wiring.Wiring], state_dir: Path
) -> None:
    """The property that makes 'resolve, do not repeat' available at all."""
    park(opened, state_dir)
    decide(opened, state_dir)

    killed = worker(opened, state_dir, kill=True)
    with pytest.raises(SimulatedCrash):
        resume(RUN_ID, V7, killed.workflow)
    killed.close()

    # The money moved. Nothing recorded that it did.
    after_crash = worker(opened, state_dir)
    assert after_crash.service.total_cents(ORDER) == AMOUNT_CENTS
    assert [i.tool for i in after_crash.ledger.unresolved(RUN_ID)] == [
        "issue_refund"
    ]
    recomputed = derived_key(RUN_ID, STEP_REFUND)
    assert after_crash.service.lookup(recomputed) is not None
    after_crash.close()


def test_the_run_survives_the_crash_with_exactly_one_refund(
    opened: list[wiring.Wiring], state_dir: Path
) -> None:
    """The property the artifact exists to prove."""
    park(opened, state_dir)
    decide(opened, state_dir)

    killed = worker(opened, state_dir, kill=True)
    with pytest.raises(SimulatedCrash):
        resume(RUN_ID, V7, killed.workflow)
    killed.close()

    third = worker(opened, state_dir)
    outcome = resume(RUN_ID, V7, third.workflow)

    assert outcome.outcome == "dispatched"
    assert outcome.phase is RunPhase.SUCCEEDED
    assert len(third.service.settlements(order_id=ORDER, kind="refund")) == 1
    assert third.service.total_cents(ORDER) == AMOUNT_CENTS
    assert third.ledger.unresolved(RUN_ID) == []
    # Friday's notice was replayed and recognised, not sent again.
    assert len(third.service.settlements(kind="message")) == 1


def test_a_nonce_pays_twice_and_notifies_twice(
    opened: list[wiring.Wiring], state_dir: Path
) -> None:
    """The failure the derivation prevents, kept reproducible."""
    park(opened, state_dir, strategy="generated")
    decide(opened, state_dir)

    killed = worker(opened, state_dir, strategy="generated", kill=True)
    with pytest.raises(SimulatedCrash):
        resume(RUN_ID, V7, killed.workflow)
    killed.close()

    third = worker(opened, state_dir, strategy="generated")
    resume(RUN_ID, V7, third.workflow)

    assert len(third.service.settlements(order_id=ORDER, kind="refund")) == 2
    assert third.service.total_cents(ORDER) == 2 * AMOUNT_CENTS
    assert len(third.service.settlements(kind="message")) == 2


# ------------------------------------------------- version before everything


def test_an_additive_change_invalidates_a_human_decision(
    opened: list[wiring.Wiring], state_dir: Path
) -> None:
    """Schema-compatible, semantically incompatible, and the incident."""
    park(opened, state_dir)
    approved_fingerprint = decide(opened, state_dir)

    deployed = worker(opened, state_dir, version=V8)
    outcome = resume(RUN_ID, V8, deployed.workflow)

    assert outcome.version_plan == "migrate"
    assert outcome.outcome == "stale_fingerprint"
    assert outcome.phase is RunPhase.WAITING_APPROVAL
    assert "notify_channel" in outcome.diff
    assert deployed.service.total_cents(ORDER) == 0

    reopened = deployed.approvals.pending(RUN_ID)
    assert reopened is not None
    assert reopened.fingerprint != approved_fingerprint
    assert reopened.decided is False


def test_the_migrated_run_finishes_once_after_a_second_decision(
    opened: list[wiring.Wiring], state_dir: Path
) -> None:
    """The stale branch is a state, not a dead end."""
    park(opened, state_dir)
    decide(opened, state_dir)

    deployed = worker(opened, state_dir, version=V8)
    resume(RUN_ID, V8, deployed.workflow)
    deployed.close()

    decide(opened, state_dir)
    finisher = worker(opened, state_dir, version=V8)
    outcome = resume(RUN_ID, V8, finisher.workflow)

    assert outcome.outcome == "dispatched"
    assert len(finisher.service.settlements(order_id=ORDER, kind="refund")) == 1
    assert finisher.service.total_cents(ORDER) == AMOUNT_CENTS


def test_an_undeclared_version_is_refused_rather_than_guessed(
    opened: list[wiring.Wiring], state_dir: Path
) -> None:
    """Three answers, and no 'probably fine'."""
    park(opened, state_dir)
    decide(opened, state_dir)

    stranger = worker(opened, state_dir, version="v9")
    with pytest.raises(ConfigDrift):
        resume(RUN_ID, "v9", stranger.workflow)

    assert stranger.store.load_envelope(RUN_ID).phase is RunPhase.FAILED
    assert stranger.service.total_cents(ORDER) == 0


def test_the_version_plan_has_exactly_three_answers() -> None:
    """Pin, migrate, or refuse. A migration nobody wrote is not a plan."""
    assert plan(V7, V7) == "pin"
    assert plan(V7, V8) == "migrate"
    assert plan(V8, "v9") == "refuse"

    stranded = Envelope(
        state=RunState(run_id=RUN_ID, step=STEP_REFUND), agent_version="v9"
    )
    with pytest.raises(NoMigrationPath):
        run_migration(stranded, to="v10")


def test_the_config_hash_covers_the_prompt_not_the_container_tag() -> None:
    """Two workers with the same hash behave the same on one checkpoint."""
    base = {
        "agent_version": V7,
        "system_prompt": "northstar-support/v1",
        "tool_versions": {"issue_refund": "3"},
        "policy_bundle": "northstar-refunds/2026-07",
        "model": "fake-model-1",
    }
    assert config_hash(**base) == config_hash(**base)
    assert config_hash(**{**base, "system_prompt": "v1 "}) != config_hash(**base)


# ------------------------------------------------------ decision, then not


def test_a_rejection_is_terminal_and_moves_no_money(
    opened: list[wiring.Wiring], state_dir: Path
) -> None:
    """Rejection has a defined next state, chosen by policy."""
    park(opened, state_dir)
    decide(opened, state_dir, approved=False)

    wired = worker(opened, state_dir)
    outcome = resume(RUN_ID, V7, wired.workflow)

    assert outcome.outcome == "rejected"
    assert outcome.phase is RunPhase.FAILED
    assert wired.service.total_cents(ORDER) == 0


def test_an_undecided_run_goes_back_to_waiting(
    opened: list[wiring.Wiring], state_dir: Path
) -> None:
    """A resume that finds no answer parks again rather than proceeding."""
    park(opened, state_dir)

    wired = worker(opened, state_dir)
    outcome = resume(RUN_ID, V7, wired.workflow)

    assert outcome.outcome == "undecided"
    assert outcome.phase is RunPhase.WAITING_APPROVAL
    assert wired.service.total_cents(ORDER) == 0


# ------------------------------------------------------- the state machine


def test_terminal_means_terminal() -> None:
    """A run record that can leave a terminal state destroys the ledger."""
    assert TERMINAL == {
        RunPhase.SUCCEEDED,
        RunPhase.FAILED,
        RunPhase.CANCELLED,
    }
    for phase in TERMINAL:
        with pytest.raises(IllegalTransition):
            check_transition(phase, RunPhase.RUNNING)


def test_the_transitions_an_incident_teaches_you_are_declared() -> None:
    """Lease expiry, expiry to failed, and a stale approval coming back."""
    check_transition(RunPhase.RUNNING, RunPhase.QUEUED)
    check_transition(RunPhase.WAITING_APPROVAL, RunPhase.FAILED)
    check_transition(RunPhase.RESUMING, RunPhase.WAITING_APPROVAL)
    check_transition(RunPhase.SUSPENDED, RunPhase.RESUMING)
    with pytest.raises(IllegalTransition):
        check_transition(RunPhase.QUEUED, RunPhase.SUCCEEDED)


def test_only_running_and_resuming_burn_anything() -> None:
    """Which is why a sixty-one-hour wait costs storage."""
    burning = {p for p in RunPhase if holds_compute(p)}
    assert burning == {RunPhase.RUNNING, RunPhase.RESUMING}


# ------------------------------------------------------------ small pieces


def test_the_checks_run_in_the_documented_order() -> None:
    """Version before fingerprint, fingerprint before decision."""
    assert RESUME_CHECKS == (
        "version",
        "fingerprint",
        "decision",
        "prior_steps",
        "dispatch",
    )


def test_one_changed_cent_changes_the_fingerprint() -> None:
    """The mechanism, in two lines."""
    approved = ToolCall("c1", "issue_refund", {"amount_cents": AMOUNT_CENTS})
    tampered = ToolCall(
        "c1", "issue_refund", {"amount_cents": AMOUNT_CENTS + 1}
    )
    assert fingerprint_of(approved, RUN_ID) != fingerprint_of(tampered, RUN_ID)
    assert argument_diff(approved.arguments, tampered.arguments) == {
        "amount_cents": {"approved": AMOUNT_CENTS, "now": AMOUNT_CENTS + 1}
    }


def test_the_key_reaches_the_call_that_carries_it() -> None:
    """A key the target never sees guarantees nothing."""
    call = with_key(ToolCall("c1", "issue_refund", {"order_id": ORDER}), "k1")
    assert call.arguments["idempotency_key"] == "k1"
    assert call.arguments["order_id"] == ORDER


def test_the_scenario_matches_the_chapter() -> None:
    """The numbers the chapter prints are the numbers here."""
    assert REFUND_APPROVAL_THRESHOLD_CENTS == 5000
    assert AMOUNT_CENTS == 24_000
    assert AMOUNT_CENTS > REFUND_APPROVAL_THRESHOLD_CENTS
    order = World().get_order(ORDER)
    assert order["total_cents"] == AMOUNT_CENTS
    assert "fraud_review" in order["flags"]
