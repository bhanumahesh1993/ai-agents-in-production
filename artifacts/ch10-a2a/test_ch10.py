"""The Chapter 10 properties, as assertions.

The demo prints; this fails a build. Every assertion is about behaviour --
what the peer's task store holds after a resent delegation, which system a
suspension routes to, what the lifecycle refuses -- rather than about the
text of a message.

Three groups of strings *are* asserted, because in each the string is the
protocol rather than a description of it: the eight prefixed ``TASK_STATE_*``
wire values, the absence of a top-level ``url`` or ``protocolVersion`` on a
v1.0 card, and the derived task id. Getting any of those wrong is a silent
failure in production, which is exactly the class of defect a test is for.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import json
from dataclasses import replace
from typing import Any

import demo
import pytest
from client.escalate import (
    REQUIRED_SCOPE,
    STRONG_ASSURANCE,
    WEAK_ASSURANCE,
    Delegator,
    PeerLink,
    RunBudget,
    build_delegation,
    escalate_to_specialist,
    escalation_tool,
    handoff_fields_present,
    mint_delegation,
)
from client.follow import ACTIONS, TERMINAL, RunContext, drive, handle
from client.resolve import (
    PEER_ID,
    SUPPORTED_A2A_VERSIONS,
    PeerRegistry,
    UntrustedPeer,
    load_pins,
    resolve_peer,
    sha256_of,
    verify_signature,
)
from northstar_contracts import ToolCall, World, idempotency_key
from northstar_policy import BudgetExceeded, BudgetGuard, Principal
from northstar_runtime import AgentLoop, FakeModel, ToolRegistry
from peer.adapter import (
    MAX_OPEN_TASKS_PER_TENANT,
    A2AServer,
    AdmissionRefused,
    evidence_message,
    step_up_message,
)
from peer.fraud_review import (
    CARD_PATH,
    EVIDENCE_ARTIFACT,
    EVIDENCE_THRESHOLD_CENTS,
    STEP_UP_SCOPE,
    FraudReviewAgent,
    pre_1_0_card,
    sign_card,
)
from transport import WELL_KNOWN_PATH, MockTransport, NoRoute, origin_of
from wire import (
    APPROVAL_THRESHOLD_CENTS,
    INITIAL_STATES,
    LEGAL_TRANSITIONS,
    SHORT_LABELS,
    TASK_STATE_AUTH_REQUIRED,
    TASK_STATE_CANCELED,
    TASK_STATE_COMPLETED,
    TASK_STATE_FAILED,
    TASK_STATE_INPUT_REQUIRED,
    TASK_STATE_REJECTED,
    TASK_STATE_SUBMITTED,
    TASK_STATE_WORKING,
    TERMINAL_STATES,
    WIRE_STATES,
    AgentCard,
    IllegalTransition,
    Interface,
    MalformedCard,
    Task,
    advance,
    is_terminal,
    require_wire_state,
    short_label,
    wire_value,
)
from wiring import wire_link  # noqa: I001

FRAUD_ORDER = "NR-2026-0042110"     # 24,000 cents, shipped, flagged
SMALL_ORDER = "NR-2026-0041827"     # 8,400 cents, delivered, lamp shade 3,250
RUN_ID = "run-ch10-a2a"
TENANT = "northstar-us"
OTHER_TENANT = "northstar-eu"


def delegate(
    link: PeerLink,
    card: AgentCard,
    *,
    order_id: str = FRAUD_ORDER,
    step_id: int | str = 4,
    assurance: str = STRONG_ASSURANCE,
) -> dict[str, Any]:
    """Send one delegation on this test's own wiring."""
    return escalate_to_specialist(
        order_id,
        "fraud_suspected",
        RUN_ID,
        step_id,
        link=link,
        assurance=assurance,
    )


# ------------------------------------------------------- the version defects
#
# These four exist because an external audit found them. A card with a
# top-level ``url`` and a client comparing against ``"completed"`` are both
# well-formed Python that fails quietly, so each one gets its own assertion.


def test_the_card_has_no_top_level_url_or_protocol_version() -> None:
    """Endpoint and version live per-interface in v1.0, not on the card."""
    body = json.loads(CARD_PATH.read_text())
    assert "url" not in body
    assert "protocolVersion" not in body
    assert "preferredTransport" not in body
    assert "additionalInterfaces" not in body
    entry = body["supportedInterfaces"][0]
    assert set(entry) == {"url", "protocolBinding", "protocolVersion"}


def test_a_pre_1_0_card_is_refused_rather_than_read_around() -> None:
    """Reading around a legacy field is how you integrate untested versions."""
    with pytest.raises(MalformedCard) as caught:
        AgentCard.from_dict(pre_1_0_card())
    message = str(caught.value)
    assert "url" in message
    assert "supportedInterfaces" in message


def test_preference_is_position_in_supported_interfaces() -> None:
    """There is no ``preferredTransport``; order carries the preference."""
    card = AgentCard(
        name="p",
        description="",
        version="1",
        supported_interfaces=(
            Interface("https://p.example/grpc", "GRPC", "1.0"),
            Interface("https://p.example/rpc", "JSONRPC", "1.0"),
        ),
    )
    assert card.preferred_interface.protocol_binding == "GRPC"
    assert card.url == "https://p.example/grpc"
    chosen = card.interface_for(binding="JSONRPC", versions=frozenset({"1.0"}))
    assert chosen is not None
    assert chosen.url == "https://p.example/rpc"
    assert card.interface_for(binding="HTTP_JSON") is None


def test_wire_states_are_prefixed_and_labels_are_not_wire_values() -> None:
    """A lowercase label never matches, so it must never be accepted."""
    assert len(WIRE_STATES) == 8
    assert all(s.startswith("TASK_STATE_") for s in WIRE_STATES)
    assert TERMINAL_STATES == {
        TASK_STATE_COMPLETED,
        TASK_STATE_FAILED,
        TASK_STATE_CANCELED,
        TASK_STATE_REJECTED,
    }
    for state in WIRE_STATES:
        assert require_wire_state(state) == state
        assert wire_value(short_label(state)) == state
    with pytest.raises(ValueError, match="human label"):
        require_wire_state("completed")
    with pytest.raises(ValueError, match="human label"):
        advance("submitted", "working")


def test_the_client_terminal_set_matches_the_wire_contract() -> None:
    """``follow.TERMINAL`` and ``wire.TERMINAL_STATES`` cannot drift apart."""
    assert TERMINAL == set(TERMINAL_STATES)
    for state in TERMINAL:
        assert is_terminal(state)


# ------------------------------------------------------------ the trust policy


def test_resolution_returns_the_pinned_card(link: PeerLink) -> None:
    """The happy path, so the refusals below mean something."""
    card = resolve_peer(PEER_ID, link.registry)
    pin = link.registry.pinned[PEER_ID]
    assert card.name == PEER_ID
    assert verify_signature(card, pin.public_key)
    assert sha256_of(card) == pin.card_hash
    assert card.protocol_version in SUPPORTED_A2A_VERSIONS


def test_a_tampered_card_fails_signature_verification(
    link: PeerLink,
) -> None:
    """TLS authenticates the host. It says nothing about the claim."""
    pin = link.registry.pinned[PEER_ID]
    link.transport.tamper(
        pin.url,
        {"skills": [{"id": "assess_claim", "description": "Issues refunds."}]},
    )
    with pytest.raises(UntrustedPeer, match="signature invalid"):
        resolve_peer(PEER_ID, link.registry)


def test_a_correctly_signed_card_still_fails_a_drifted_hash(
    link: PeerLink,
    peer: A2AServer,
) -> None:
    """The case a signature alone cannot catch: the peer changed.

    Everything verifies. The card is simply not the one anybody reviewed,
    which is a decision for a human rather than for a runtime.
    """
    pin = link.registry.pinned[PEER_ID]
    body, _ = peer.agent_card()
    changed = {**body, "version": "1.9.0"}
    signature = sign_card(AgentCard.from_dict(changed).to_dict())
    link.transport.serve_card(pin.url, changed, signature)

    served = link.registry.fetch(pin.url)
    assert verify_signature(served, pin.public_key)      # signature is fine
    with pytest.raises(UntrustedPeer, match="drifted from pin"):
        resolve_peer(PEER_ID, link.registry)


def test_an_unsupported_version_fails_loudly_at_resolution(
    link: PeerLink,
    peer: A2AServer,
) -> None:
    """Better a refusal here than a state name the client cannot parse."""
    pin = link.registry.pinned[PEER_ID]
    body, _ = peer.agent_card()
    ahead = {
        **body,
        "supportedInterfaces": [
            {
                "url": pin.url,
                "protocolBinding": "JSONRPC",
                "protocolVersion": "2.0",
            }
        ],
    }
    card = AgentCard.from_dict(ahead)
    link.transport.serve_card(pin.url, ahead, sign_card(card.to_dict()))
    link.registry.pinned = {PEER_ID: replace(pin, card_hash=sha256_of(card))}
    with pytest.raises(UntrustedPeer, match="unsupported A2A version"):
        resolve_peer(PEER_ID, link.registry)


def test_an_unreviewed_peer_has_no_pin_and_is_not_resolved(
    link: PeerLink,
) -> None:
    """A registry is an allowlist. Absence is a refusal, not a lookup miss."""
    with pytest.raises(UntrustedPeer, match="no pin"):
        resolve_peer("acme-refund-bot", link.registry)


def test_a_card_that_dropped_the_skill_is_refused(
    link: PeerLink,
    peer: A2AServer,
) -> None:
    """A verified signature over a card you cannot use is still unusable."""
    pin = link.registry.pinned[PEER_ID]
    body, _ = peer.agent_card()
    without = {**body, "skills": []}
    card = AgentCard.from_dict(without)
    link.transport.serve_card(pin.url, without, sign_card(card.to_dict()))
    link.registry.pinned = {PEER_ID: replace(pin, card_hash=sha256_of(card))}
    with pytest.raises(UntrustedPeer, match="no longer advertises"):
        resolve_peer(PEER_ID, link.registry)


def test_the_pinned_hash_covers_the_card_on_disk() -> None:
    """The deployed pin and the published card agree, or the build fails.

    This is what a pin is for. Editing ``cards/fraud-review.json`` without
    updating ``client/pins.json`` is drift, and drift is supposed to be
    caught here rather than at three in the morning.
    """
    pins = load_pins()
    card = AgentCard.from_dict(json.loads(CARD_PATH.read_text()))
    assert sha256_of(card) == pins[PEER_ID].card_hash
    assert card.preferred_interface.url == pins[PEER_ID].url


def test_the_card_is_published_at_the_well_known_path(
    link: PeerLink,
) -> None:
    """Discovery is a GET at a path nobody has to agree on by email."""
    pin = link.registry.pinned[PEER_ID]
    assert link.registry.card_url(pin.url).endswith(WELL_KNOWN_PATH)
    assert origin_of(pin.url) == "https://fraud-review.internal.example"
    with pytest.raises(NoRoute):
        MockTransport().fetch_card("https://nobody.example/a2a")


# -------------------------------------------------------- the eight states


def test_every_legal_transition_is_accepted() -> None:
    """The graph is the specification; walk all of it."""
    for state, allowed in LEGAL_TRANSITIONS.items():
        for target in allowed:
            assert advance(state, target) == target


def test_every_illegal_transition_is_refused() -> None:
    """Including the four terminal states, which reach nothing at all."""
    refused = 0
    for state in WIRE_STATES:
        for target in WIRE_STATES:
            if target in LEGAL_TRANSITIONS[state]:
                continue
            with pytest.raises(IllegalTransition):
                advance(state, target)
            refused += 1
    assert refused == 8 * 8 - sum(len(v) for v in LEGAL_TRANSITIONS.values())
    for state in TERMINAL_STATES:
        assert LEGAL_TRANSITIONS[state] == frozenset()


def test_a_task_can_only_be_created_submitted_or_rejected() -> None:
    """``rejected`` is an admission outcome, never a move out of ``submitted``."""
    assert INITIAL_STATES == {TASK_STATE_SUBMITTED, TASK_STATE_REJECTED}
    assert Task(id="t", tenant=TENANT, skill="assess_claim").state == (
        TASK_STATE_SUBMITTED
    )
    for state in WIRE_STATES:
        if state in INITIAL_STATES:
            continue
        with pytest.raises(IllegalTransition):
            Task(id="t", tenant=TENANT, skill="assess_claim", state=state)


def test_a_terminal_task_accepts_no_further_messages() -> None:
    """Only the non-terminal states take new messages."""
    task = Task(id="t", tenant=TENANT, skill="assess_claim")
    task.move_to(TASK_STATE_WORKING)
    task.move_to(TASK_STATE_COMPLETED, note="done")
    assert task.messages[-1]["text"] == "done"
    with pytest.raises(IllegalTransition, match="terminal"):
        task.add_message("user", "one more thing")


# ------------------------------------------------------- delegation and retry


def test_the_task_id_is_derived_from_the_run_and_step(
    link: PeerLink,
    card: AgentCard,
) -> None:
    """A random id per attempt is a nonce, not an identifier."""
    task = delegate(link, card, step_id=4)
    assert task["id"] == idempotency_key(run_id=RUN_ID, step_id=4)
    other = delegate(link, card, step_id=5)
    assert other["id"] != task["id"]


def test_a_resent_delegation_rejoins_instead_of_opening_a_second_review(
    link: PeerLink,
    card: AgentCard,
    peer: A2AServer,
) -> None:
    """The property the chapter is about. One task, one review, one hold."""
    first = delegate(link, card)
    again = delegate(link, card)
    assert again["id"] == first["id"]
    assert len(peer.tasks_for(TENANT)) == 1
    assert peer.reviews_opened == 1
    assert [r["outcome"] for r in peer.audit] == ["accepted", "rejoined"]


def test_a_resent_delegation_rejoins_a_task_already_in_flight(
    link: PeerLink,
    card: AgentCard,
    peer: A2AServer,
    ctx: RunContext,
) -> None:
    """Rejoining does not restart the work or reset the state."""
    task = delegate(link, card)
    _, task, _ = drive(
        task, ctx, transport=link.transport, card=card, tenant=TENANT
    )
    assert task["state"] == TASK_STATE_INPUT_REQUIRED
    rejoined = delegate(link, card)
    assert rejoined["state"] == TASK_STATE_INPUT_REQUIRED
    assert peer.reviews_opened == 1
    assert peer.checks_run == 1


def test_a_new_task_starts_submitted_and_only_then_works(
    link: PeerLink,
    card: AgentCard,
) -> None:
    """Splitting the two is what tells a safe retry from a dangerous one."""
    task = delegate(link, card)
    assert task["state"] == TASK_STATE_SUBMITTED
    task = link.transport.get_task(card, task["id"], tenant=TENANT)
    assert task["state"] == TASK_STATE_WORKING
    assert task["history"][:2] == [TASK_STATE_SUBMITTED, TASK_STATE_WORKING]


# -------------------------------------------------- the four client branches


def test_input_required_suspends_and_asks_the_customer(
    link: PeerLink,
    card: AgentCard,
    ctx: RunContext,
) -> None:
    """The 08:50 state, and the one the old table could not express."""
    task = delegate(link, card)
    action, task, states = drive(
        task, ctx, transport=link.transport, card=card, tenant=TENANT
    )
    assert action == "suspend"
    assert task["state"] == TASK_STATE_INPUT_REQUIRED
    assert states == [
        TASK_STATE_SUBMITTED,
        TASK_STATE_WORKING,
        TASK_STATE_INPUT_REQUIRED,
    ]
    assert len(ctx.asked) == 1
    assert EVIDENCE_ARTIFACT in ctx.asked[0]["text"]
    assert ctx.step_ups == []


def test_auth_required_suspends_and_asks_the_authorization_server(
    link: PeerLink,
    card: AgentCard,
    ctx: RunContext,
) -> None:
    """Merging the two blocks asks a customer for an OAuth grant."""
    task = delegate(link, card, assurance=WEAK_ASSURANCE)
    action, task, _ = drive(
        task, ctx, transport=link.transport, card=card, tenant=TENANT
    )
    assert action == "suspend"
    assert task["state"] == TASK_STATE_AUTH_REQUIRED
    assert ctx.step_ups == [[STEP_UP_SCOPE]]
    assert ctx.asked == []


def test_rejected_is_terminal_and_did_no_domain_work(
    link: PeerLink,
    card: AgentCard,
    peer: A2AServer,
    ctx: RunContext,
) -> None:
    """A rejected task will be rejected again, so retrying it is waste."""
    task = delegate(link, card, order_id="NR-2026-9999999")
    assert task["state"] == TASK_STATE_REJECTED
    assert handle(task, ctx) == "finish"
    assert peer.checks_run == 0
    assert peer.reviews_opened == 0


def test_completed_finishes_and_carries_the_verdict(
    link: PeerLink,
    card: AgentCard,
    ctx: RunContext,
) -> None:
    """The whole path, including the answer that unblocks it."""
    task = delegate(link, card)
    _, task, _ = drive(
        task, ctx, transport=link.transport, card=card, tenant=TENANT
    )
    task = link.transport.send_message(
        card, task["id"], evidence_message(), tenant=TENANT
    )
    action, task, states = drive(
        task, ctx, transport=link.transport, card=card, tenant=TENANT
    )
    assert action == "finish"
    assert task["state"] == TASK_STATE_COMPLETED
    assert states == [TASK_STATE_WORKING, TASK_STATE_COMPLETED]
    verdict = task["artifacts"][0]["content"]
    assert verdict["order_id"] == FRAUD_ORDER
    assert verdict["claim_cents"] == 24000
    assert verdict["evidence"] == [EVIDENCE_ARTIFACT]


def test_handle_returns_nothing_but_the_three_actions(
    link: PeerLink,
    card: AgentCard,
    ctx: RunContext,
) -> None:
    """Four branches, three outcomes, and no fourth value leaking out."""
    seen = set()
    for state in WIRE_STATES:
        fake = {
            "id": "t",
            "state": state,
            "messages": [{"role": "agent", "text": "x"}],
            "required_scopes": [],
        }
        seen.add(handle(fake, ctx))
    assert seen <= ACTIONS
    assert seen == {"finish", "suspend", "await"}


def test_a_cancelled_task_is_terminal(
    link: PeerLink,
    card: AgentCard,
    ctx: RunContext,
) -> None:
    """Cancel is a legal move out of every non-terminal state."""
    task = delegate(link, card)
    task = link.transport.cancel_task(card, task["id"], tenant=TENANT)
    assert task["state"] == TASK_STATE_CANCELED
    assert handle(task, ctx) == "finish"
    with pytest.raises(IllegalTransition):
        link.transport.cancel_task(card, task["id"], tenant=TENANT)


def test_the_wrong_answer_to_a_block_is_an_error_not_a_no_op(
    link: PeerLink,
    card: AgentCard,
    ctx: RunContext,
) -> None:
    """Uploading a photo to clear an authorization block goes nowhere."""
    task = delegate(link, card, assurance=WEAK_ASSURANCE)
    _, task, _ = drive(
        task, ctx, transport=link.transport, card=card, tenant=TENANT
    )
    assert task["state"] == TASK_STATE_AUTH_REQUIRED
    with pytest.raises(IllegalTransition):
        link.transport.send_message(
            card, task["id"], evidence_message(), tenant=TENANT
        )
    task = link.transport.send_message(
        card, task["id"], step_up_message(STRONG_ASSURANCE), tenant=TENANT
    )
    assert task["state"] == TASK_STATE_WORKING
    assert task["required_scopes"] == []


# ------------------------------------------------------ identity and tenancy


def test_all_six_handoff_fields_travel(link: PeerLink) -> None:
    """In-process they were good practice. Here they are the contract."""
    delegation = build_delegation(
        FRAUD_ORDER,
        "fraud_suspected",
        "task-1",
        run_id=RUN_ID,
        step_id=4,
        link=link,
    )
    assert handoff_fields_present(delegation) == []
    assert delegation["constraints"]["approval_threshold_cents"] == (
        APPROVAL_THRESHOLD_CENTS
    )
    assert delegation["provenance"]["run_id"] == RUN_ID
    assert delegation["provenance"]["step_id"] == "4"


def test_an_incomplete_handoff_is_rejected(
    link: PeerLink,
    card: AgentCard,
) -> None:
    """Each of the six, dropped one at a time."""
    base = build_delegation(
        FRAUD_ORDER,
        "fraud_suspected",
        "task-1",
        run_id=RUN_ID,
        step_id=4,
        link=link,
    )
    for i, field in enumerate(("goal", "constraints", "state_ref",
                               "budget_remaining", "provenance",
                               "return_contract")):
        payload = {k: v for k, v in base.items() if k != field}
        payload["task_id"] = f"task-{i}"
        task = link.transport.send_task(card, payload)
        assert task["state"] == TASK_STATE_REJECTED, field


def test_the_budget_travels_as_a_remainder(link: PeerLink) -> None:
    """A fresh allowance per hop spends the budget once per agent."""
    link.budget.spend(35)
    delegation = build_delegation(
        FRAUD_ORDER,
        "fraud_suspected",
        "task-1",
        run_id=RUN_ID,
        step_id=4,
        link=link,
    )
    assert delegation["budget_remaining"] == 165
    link.budget.spend(100)
    later = build_delegation(
        FRAUD_ORDER,
        "fraud_suspected",
        "task-2",
        run_id=RUN_ID,
        step_id=5,
        link=link,
    )
    assert later["budget_remaining"] == 65


def test_a_forwarded_credential_is_refused(
    link: PeerLink,
    card: AgentCard,
) -> None:
    """A receiver holding the sender's token is a confused deputy."""
    delegation = build_delegation(
        FRAUD_ORDER,
        "fraud_suspected",
        "task-1",
        run_id=RUN_ID,
        step_id=4,
        link=link,
    )
    for key in ("access_token", "bearer", "authorization", "id_token"):
        hostile = {
            **delegation,
            "auth": {**delegation["auth"], key: "the-support-session"},
        }
        with pytest.raises(AdmissionRefused, match="delegation"):
            link.transport.send_task(card, hostile)


def test_the_minted_grant_carries_no_credential(link: PeerLink) -> None:
    """What is in the grant is a claim set, a scope list, and an expiry."""
    grant = mint_delegation(link.principal, REQUIRED_SCOPE, now=100.0)
    assert grant["kind"] == "delegation"
    assert grant["scopes"] == [REQUIRED_SCOPE]
    assert grant["audience"] == PEER_ID
    assert grant["expires_at"] > grant["issued_at"]
    assert grant["chain"] == [
        "northstar-platform",
        "northstar-support-agent",
    ]
    assert not {"access_token", "bearer", "token"} & set(grant)


def test_an_expired_or_misaddressed_grant_is_refused(
    link: PeerLink,
    card: AgentCard,
) -> None:
    """Short-lived and audience-bound, or it is a credential again."""
    delegation = build_delegation(
        FRAUD_ORDER,
        "fraud_suspected",
        "task-1",
        run_id=RUN_ID,
        step_id=4,
        link=link,
    )
    expired = {
        **delegation,
        "auth": {**delegation["auth"], "expires_at": -1.0},
    }
    with pytest.raises(AdmissionRefused, match="expired"):
        link.transport.send_task(card, expired)
    elsewhere = {
        **delegation,
        "auth": {**delegation["auth"], "audience": "acme-refunds"},
    }
    with pytest.raises(AdmissionRefused, match="addressed to"):
        link.transport.send_task(card, elsewhere)


def test_the_tenant_must_travel_and_is_never_inferred(
    link: PeerLink,
    card: AgentCard,
) -> None:
    """The credential does not name the tenant, and must not have to."""
    delegation = build_delegation(
        FRAUD_ORDER,
        "fraud_suspected",
        "task-1",
        run_id=RUN_ID,
        step_id=4,
        link=link,
    )
    assert "tenant" not in delegation["auth"]
    with pytest.raises(AdmissionRefused, match="no tenant"):
        link.transport.send_task(card, {**delegation, "tenant": ""})


def test_a_client_suggested_task_id_is_namespaced_by_tenant(
    link: PeerLink,
    card: AgentCard,
    peer: A2AServer,
) -> None:
    """Otherwise one caller can read another caller's task by guessing an id."""
    delegation = build_delegation(
        FRAUD_ORDER,
        "fraud_suspected",
        "shared-id",
        run_id=RUN_ID,
        step_id=4,
        link=link,
    )
    link.transport.send_task(card, delegation)
    link.transport.send_task(card, {**delegation, "tenant": OTHER_TENANT})
    assert len(peer.tasks_for(TENANT)) == 1
    assert len(peer.tasks_for(OTHER_TENANT)) == 1
    assert peer.reviews_opened == 2
    with pytest.raises(AdmissionRefused):
        peer.get_task("northstar-apac", "shared-id")


def test_a_tenant_over_quota_is_rejected(
    link: PeerLink,
    card: AgentCard,
) -> None:
    """A quota is what stands between one noisy caller and everyone else."""
    states = []
    for step in range(MAX_OPEN_TASKS_PER_TENANT + 2):
        task = delegate(link, card, step_id=f"quota-{step}")
        states.append(task["state"])
    assert states[:MAX_OPEN_TASKS_PER_TENANT] == (
        [TASK_STATE_SUBMITTED] * MAX_OPEN_TASKS_PER_TENANT
    )
    assert states[MAX_OPEN_TASKS_PER_TENANT:] == [TASK_STATE_REJECTED] * 2


def test_the_peer_enforces_the_constraint_that_travelled(
    link: PeerLink,
    card: AgentCard,
    ctx: RunContext,
) -> None:
    """A restated constraint the receiver ignores is decoration."""
    task = delegate(link, card)
    _, task, _ = drive(
        task, ctx, transport=link.transport, card=card, tenant=TENANT
    )
    task = link.transport.send_message(
        card, task["id"], evidence_message(), tenant=TENANT
    )
    _, task, _ = drive(
        task, ctx, transport=link.transport, card=card, tenant=TENANT
    )
    verdict = task["artifacts"][0]["content"]
    assert verdict["approval_threshold_cents"] == APPROVAL_THRESHOLD_CENTS
    assert verdict["requires_approval"] is True


def test_a_delegation_without_the_threshold_is_rejected(
    link: PeerLink,
    card: AgentCard,
) -> None:
    """The receiver cannot enforce a rule that did not arrive."""
    delegation = build_delegation(
        FRAUD_ORDER,
        "fraud_suspected",
        "task-1",
        run_id=RUN_ID,
        step_id=4,
        link=link,
    )
    delegation["constraints"] = {"reason": "fraud_suspected"}
    task = link.transport.send_task(card, delegation)
    assert task["state"] == TASK_STATE_REJECTED
    assert "approval_threshold_cents" in task["messages"][-1]["text"]


# ------------------------------------------------------------- the two halves


def test_a_claim_below_the_evidence_threshold_needs_no_photograph(
    link: PeerLink,
    card: AgentCard,
    ctx: RunContext,
) -> None:
    """``input_required`` is a judgement the peer makes, not a fixed step."""
    task = delegate(link, card, order_id=SMALL_ORDER, step_id=21)
    action, task, states = drive(
        task, ctx, transport=link.transport, card=card, tenant=TENANT
    )
    assert action == "finish"
    assert task["state"] == TASK_STATE_COMPLETED
    assert TASK_STATE_INPUT_REQUIRED not in states
    verdict = task["artifacts"][0]["content"]
    assert verdict["claim_cents"] == 8400
    assert verdict["claim_cents"] < EVIDENCE_THRESHOLD_CENTS
    assert verdict["requires_approval"] is True
    assert ctx.asked == []


def test_the_two_agents_do_not_share_a_world(
    link: PeerLink,
    card: AgentCard,
    peer: A2AServer,
    ctx: RunContext,
) -> None:
    """Refunding on one side does not change what the other side reads."""
    caller_world = World()
    caller_world.issue_refund(
        SMALL_ORDER, 3250, "damaged", idempotency_key="k1"
    )
    assert caller_world.total_refunded_cents(SMALL_ORDER) == 3250
    assert peer.agent.world.total_refunded_cents(SMALL_ORDER) == 0
    task = delegate(link, card, order_id=SMALL_ORDER, step_id=21)
    _, task, _ = drive(
        task, ctx, transport=link.transport, card=card, tenant=TENANT
    )
    assert task["artifacts"][0]["content"]["claim_cents"] == 8400


def test_the_peer_never_moves_money(
    link: PeerLink,
    card: AgentCard,
    peer: A2AServer,
    ctx: RunContext,
) -> None:
    """The delegation says ``may_move_money: False`` and the peer honours it."""
    task = delegate(link, card)
    _, task, _ = drive(
        task, ctx, transport=link.transport, card=card, tenant=TENANT
    )
    task = link.transport.send_message(
        card, task["id"], evidence_message(), tenant=TENANT
    )
    drive(task, ctx, transport=link.transport, card=card, tenant=TENANT)
    assert peer.agent.world.refunds == []
    assert peer.agent.world.effects("refund_issued") == []


def test_the_peer_graph_does_not_redo_work_on_resume(
    link: PeerLink,
    card: AgentCard,
    peer: A2AServer,
    ctx: RunContext,
) -> None:
    """A resumed task re-enters the graph; it does not re-run its checks."""
    task = delegate(link, card)
    _, task, _ = drive(
        task, ctx, transport=link.transport, card=card, tenant=TENANT
    )
    assert peer.checks_run == 1
    task = link.transport.send_message(
        card, task["id"], evidence_message(), tenant=TENANT
    )
    drive(task, ctx, transport=link.transport, card=card, tenant=TENANT)
    assert peer.checks_run == 1


def test_the_agent_holds_no_reference_to_the_client(peer: A2AServer) -> None:
    """A fresh agent is usable with nothing from ``client/`` imported."""
    agent = FraudReviewAgent()
    assert agent.declines("assess_claim", {"order_id": FRAUD_ORDER,
                                           "approval_threshold_cents": 5000}) is None
    assert agent.declines("issue_refund", {"order_id": FRAUD_ORDER,
                                           "approval_threshold_cents": 5000})
    assert agent.reviews_opened == 0
    assert isinstance(peer.agent, FraudReviewAgent)


# --------------------------------------------------------- inside the loop


def test_the_same_named_tool_delegates_from_inside_the_loop() -> None:
    """The model cannot tell that the implementation crosses a boundary."""
    server = A2AServer()
    link, _ = wire_link(server=server)
    world = World()
    registry = ToolRegistry(inject_idempotency_key=True)
    for spec, fn in world.tools():
        if spec.name != "escalate_to_specialist":
            registry.register(spec, fn)
    registry.register(*escalation_tool(link, RUN_ID))

    model = FakeModel(
        default=[
            ToolCall("c1", "get_order", {"order_id": FRAUD_ORDER}),
            ToolCall(
                "c2",
                "escalate_to_specialist",
                {"order_id": FRAUD_ORDER, "reason": "fraud_suspected"},
            ),
            "The fraud team has it and needs a photograph.",
        ]
    )
    state = AgentLoop(model=model, tools=registry, max_turns=6).run(
        f"Customer disputes {FRAUD_ORDER}, flagged for review.",
        run_id=RUN_ID,
    )
    called = [c.name for m in state.messages for c in m.tool_calls]
    assert called == ["get_order", "escalate_to_specialist"]
    assert state.status == "succeeded"
    assert server.reviews_opened == 1
    assert world.refunds == []


def test_a_replayed_step_rejoins_its_own_task() -> None:
    """The registry's stamp is the step id, so the task id is stable."""
    server = A2AServer()
    link, _ = wire_link(server=server)
    registry = ToolRegistry(inject_idempotency_key=True)
    spec, fn = escalation_tool(link, RUN_ID)
    registry.register(spec, fn)
    call = ToolCall(
        "c2",
        "escalate_to_specialist",
        {"order_id": FRAUD_ORDER, "reason": "fraud_suspected"},
    )
    first = registry.dispatch(call, run_id=RUN_ID, step=2)
    second = registry.dispatch(call, run_id=RUN_ID, step=2)
    assert first.ok and second.ok
    assert first.content["id"] == second.content["id"]
    assert server.reviews_opened == 1
    assert registry.is_retry_safe(call) is True


def test_the_escalation_tool_declares_what_it_is(link: PeerLink) -> None:
    """A remote delegation is a write, and it is idempotent by derivation."""
    spec, _ = escalation_tool(link, RUN_ID)
    assert spec.name == "escalate_to_specialist"
    assert spec.writes is True
    assert spec.idempotent is True
    assert "idempotency_key" in spec.input_schema["properties"]
    assert spec.input_schema["additionalProperties"] is False
    assert "run_id" not in spec.input_schema["properties"]
    assert "step_id" not in spec.input_schema["properties"]


# ---------------------------------------------------------------- the demo


def test_the_demo_exits_zero() -> None:
    """The printed command is the tested command."""
    assert demo.main([]) == 0


def test_the_demo_tamper_mode_exits_zero() -> None:
    """``--tamper-card`` passes only because resolution failed closed."""
    assert demo.main(["--tamper-card"]) == 0


def test_the_client_can_be_pointed_at_a_second_peer() -> None:
    """A pin is data. Adding a reviewed peer changes no code.

    Also the one place the artifact says out loud what it cannot do offline:
    this second peer is the same implementation behind a second url, so it
    demonstrates registry plumbing rather than interoperability with a
    foreign stack. Conformance against a real peer is an empirical question
    a mock cannot settle.
    """
    other_server = A2AServer()
    link, _ = wire_link()
    pin = link.registry.pinned[PEER_ID]
    second_url = "https://fraud-review-eu.internal.example/a2a"
    body, signature = other_server.agent_card()
    moved = {
        **body,
        "supportedInterfaces": [
            {
                "url": second_url,
                "protocolBinding": "JSONRPC",
                "protocolVersion": "1.0",
            }
        ],
    }
    card = AgentCard.from_dict(moved)
    link.transport.mount(second_url, other_server)
    link.transport.serve_card(second_url, moved, sign_card(card.to_dict()))
    link.registry.pinned = {
        **link.registry.pinned,
        "northstar-fraud-review-eu": replace(
            pin,
            peer_id=PEER_ID,
            url=second_url,
            card_hash=sha256_of(card),
        ),
    }
    resolved = resolve_peer("northstar-fraud-review-eu", link.registry)
    assert resolved.preferred_interface.url == second_url


def test_short_labels_cover_every_state_exactly_once() -> None:
    """Prose and code disagree about spelling, and only in one direction."""
    assert set(SHORT_LABELS) == set(WIRE_STATES)
    assert len(set(SHORT_LABELS.values())) == 8


def test_a_delegator_is_a_principal_plus_a_tenant() -> None:
    """Three identities, and the tenant is not one of them."""
    delegator = Delegator(
        principal=Principal.of("CUST-9032", REQUIRED_SCOPE),
        tenant_id=TENANT,
    )
    assert delegator.principal.user_id == "CUST-9032"
    assert delegator.principal.agent_id == "northstar-support-agent"
    assert delegator.tenant_id == TENANT
    assert delegator.scopes == frozenset({REQUIRED_SCOPE})


def test_run_budget_reports_zero_when_it_is_spent() -> None:
    """A remainder of nothing is a remainder, not an unlimited budget."""
    budget = RunBudget(BudgetGuard(max_cents=50, max_turns=None))
    assert budget.remainder() == 50
    with pytest.raises(BudgetExceeded):
        budget.spend(60)
    assert budget.remainder() == 0


def test_registry_fetch_and_pins_are_independent(link: PeerLink) -> None:
    """A registry with an empty allowlist fetches nothing."""
    empty = PeerRegistry(link.transport, pinned={})
    with pytest.raises(UntrustedPeer, match="none pinned"):
        resolve_peer(PEER_ID, empty)
