"""What the fingerprint guarantees. The shortest complete statement.

Read this file first. Four properties, and the first one is the June
incident: ten times the amount, one changed integer, no matching decision.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dataclasses import replace

import pytest
from fingerprint import ToolVersions, fingerprint
from inbox import ResumeState, TaskInbox
from northstar_contracts import ToolCall
from northstar_policy import Principal
from run import AMOUNT, ORDER, PRINCIPAL, RUN_ID, TOOL_VERSION, refund_call

OTHER_RUN = "run_ch20_someone_else"
APPROVER = "rota:fraud-review"
CURRENT = ResumeState(
    tool_version=TOOL_VERSION,
    policy_version="2026-07-01",
    world_version=f"{ORDER}:0:0",
    authorised=frozenset({APPROVER}),
)


def an_inbox(clock=None) -> TaskInbox:  # noqa: ANN001 - a tiny local factory
    """An inbox holding nothing, with Northstar's tool versions."""
    return TaskInbox(
        PRINCIPAL,
        ToolVersions(overrides={"issue_refund": TOOL_VERSION}),
        clock=clock,
    )


def approved_request(inbox: TaskInbox, call: ToolCall, run_id: str = RUN_ID):  # noqa: ANN201
    """Open a request for ``call`` and approve it. Returns the request."""
    request = inbox.request(run_id, 3, call, reason="over threshold")
    inbox.attach(request.id, {}, CURRENT)
    inbox.approve(request.id, by=APPROVER)
    return request


def test_modified_call_fails_prior_approval() -> None:
    """The one to read. One changed integer, and the decision is gone."""
    inbox = an_inbox()
    approved = refund_call(AMOUNT)
    approved_request(inbox, approved)

    tampered = replace(
        approved, arguments={**approved.arguments, "amount_cents": 240000}
    )
    approved_fp = fingerprint(approved, PRINCIPAL, RUN_ID, TOOL_VERSION)
    tampered_fp = fingerprint(tampered, PRINCIPAL, RUN_ID, TOOL_VERSION)

    assert approved_fp != tampered_fp
    assert inbox.find(approved_fp) is not None
    assert inbox.find(tampered_fp) is None
    assert inbox.is_approved(approved, RUN_ID) is True
    assert inbox.is_approved(tampered, RUN_ID) is False


def test_an_approval_from_another_run_does_not_bind() -> None:
    """``run_id`` is inside the hash, so a decision cannot be replayed."""
    inbox = an_inbox()
    call = refund_call(AMOUNT)
    approved_request(inbox, call, run_id=OTHER_RUN)

    assert inbox.is_approved(call, OTHER_RUN) is True
    assert inbox.is_approved(call, RUN_ID) is False
    assert (
        inbox.find(fingerprint(call, PRINCIPAL, RUN_ID, TOOL_VERSION)) is None
    )


def test_an_expired_approval_does_not_bind() -> None:
    """Expiry rejects. A yes from four hours ago is not consent to act now."""
    now = [1000.0]
    inbox = an_inbox(clock=lambda: now[0])
    call = refund_call(AMOUNT)
    request = approved_request(inbox, call)

    assert inbox.is_approved(call, RUN_ID) is True

    now[0] = request.expires_at + 1.0
    assert inbox.is_approved(call, RUN_ID) is False
    assert inbox.status(call, RUN_ID) == "expired"

    record = inbox.find(request.fingerprint)
    assert record is not None
    outcome = record.check(
        request.fingerprint, 3, now[0], CURRENT
    )
    assert outcome.ok is False
    assert "decision.unexpired" in {c.name for c in outcome.failed_checks}


def test_a_replay_of_the_same_step_reuses_the_decision_exactly_once() -> None:
    """Step is out of the hash and recorded here instead.

    A worker that dies and replays step 3 must not wake a specialist at
    3 a.m. to answer the same question. The same call arriving at step 5 is
    a different intent and must.
    """
    inbox = an_inbox()
    call = refund_call(AMOUNT)
    request = approved_request(inbox, call)

    assert inbox.consume(request.fingerprint, 3) is True
    assert inbox.consume(request.fingerprint, 3) is True  # the replay
    assert inbox.consume(request.fingerprint, 5) is False  # a new intent

    record = inbox.find(request.fingerprint)
    assert record is not None
    assert record.consumed_at_steps == frozenset({3})


def test_requesting_the_same_fingerprint_twice_asks_once() -> None:
    """An agent that retries must not generate a second question."""
    inbox = an_inbox()
    call = refund_call(AMOUNT)
    first = inbox.request(RUN_ID, 3, call, reason="over threshold")
    second = inbox.request(RUN_ID, 3, call, reason="over threshold")

    assert first.id == second.id
    assert len(inbox.pending()) == 1
    assert len(inbox.events_of("requested")) == 1


def test_the_six_resume_checks_run_in_order_and_report_the_diff() -> None:
    """A stale approval returns a diff, not a bare refusal."""
    inbox = an_inbox()
    call = refund_call(AMOUNT)
    request = approved_request(inbox, call)
    record = inbox.find(request.fingerprint)
    assert record is not None

    moved = ResumeState(
        tool_version="4",
        policy_version="2026-08-01",
        world_version=f"{ORDER}:1:24000",
        authorised=frozenset(),
    )
    outcome = record.check(request.fingerprint, 3, 0.0, moved)

    assert outcome.ok is False
    failed = {c.name for c in outcome.failed_checks}
    assert "approver.still_authorised" in failed
    assert "policy.still_routes_to_human" in failed
    assert "versions.unchanged" in failed
    assert "world.precondition_holds" in failed
    # The cheap checks are evaluated first, so the order of the report is
    # the order of the list rather than the order the failures happened in.
    assert [c.name for c in outcome.checks][0] == "decision.exists_and_approves"


def test_a_different_principal_does_not_share_a_fingerprint() -> None:
    """The call runs *as* someone. Change that and it is a different act."""
    call = refund_call(AMOUNT)
    mine = fingerprint(call, PRINCIPAL, RUN_ID, TOOL_VERSION)
    theirs = fingerprint(
        call,
        Principal(user_id="CUST-0001", scopes=PRINCIPAL.scopes),
        RUN_ID,
        TOOL_VERSION,
    )
    assert mine != theirs


def test_a_decided_request_cannot_be_decided_again() -> None:
    """A decision is a fact about a moment. Rewriting it destroys the trail."""
    inbox = an_inbox()
    request = approved_request(inbox, refund_call(AMOUNT))
    with pytest.raises(ValueError):
        inbox.approve(request.id, by=APPROVER)
