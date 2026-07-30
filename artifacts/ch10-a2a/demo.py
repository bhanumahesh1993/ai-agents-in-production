"""Northstar's fraud-review handoff, rebuilt on A2A, with no network at all.

    python artifacts/ch10-a2a/demo.py                 # the whole thing
    python artifacts/ch10-a2a/demo.py --tamper-card   # resolution fails closed

Two agents on two runtimes. The support agent is the book's ``AgentLoop``
with ``FakeModel``; the fraud review agent is a node graph behind an A2A
adapter, and neither imports a line of the other's code. The demo resolves
the peer's pinned card, delegates the review of order NR-2026-0042110,
drives the task through ``TASK_STATE_SUBMITTED``, ``TASK_STATE_WORKING``,
``TASK_STATE_INPUT_REQUIRED`` and ``TASK_STATE_COMPLETED``, then resubmits
the identical delegation and checks that the peer's task store still holds
exactly one task.

Exits non-zero if a resubmitted delegation opens a second review, if any of
the four blocking or terminal states routes to the wrong client action, if a
tampered or drifted or pre-1.0 card is accepted, if a forwarded credential is
accepted, if a rejected task ran any domain work, or if the eight-state
lifecycle permits a move it should not.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from dataclasses import replace
from typing import Any

from client.escalate import (
    SKILL,
    STRONG_ASSURANCE,
    WEAK_ASSURANCE,
    PeerLink,
    build_delegation,
    escalate_to_specialist,
    escalation_tool,
    mint_delegation,
)
from client.follow import RunContext, drive, handle
from client.resolve import (
    PEER_ID,
    SUPPORTED_A2A_VERSIONS,
    UntrustedPeer,
    resolve_peer,
    sha256_of,
    skill_description,
    verify_signature,
)
from northstar_contracts import ToolCall, World, idempotency_key
from northstar_runtime import AgentLoop, FakeModel, ToolRegistry
from peer.adapter import (
    AUDIENCE,
    A2AServer,
    AdmissionRefused,
    evidence_message,
    step_up_message,
)
from peer.fraud_review import EVIDENCE_ARTIFACT, STEP_UP_SCOPE, sign_card
from wire import (
    APPROVAL_THRESHOLD_CENTS,
    TASK_STATE_COMPLETED,
    TASK_STATE_INPUT_REQUIRED,
    TASK_STATE_REJECTED,
    TASK_STATE_SUBMITTED,
    TASK_STATE_WORKING,
    WIRE_STATES,
    AgentCard,
    IllegalTransition,
    MalformedCard,
    advance,
    short_label,
)
from wiring import wire_link  # noqa: I001

ORDER = "NR-2026-0042110"          # 2 x 12,000 cents, shipped, flagged
CLAIM_CENTS = 24000
RUN_ID = "run-ch10-a2a"
STEP_ID = 4
TENANT = "northstar-us"
OTHER_TENANT = "northstar-eu"


def banner(title: str) -> None:
    """Print a section header."""
    print(f"\n=== {title} ===")


# ------------------------------------------------------------------ 1. trust


def show_resolution(link: PeerLink, failures: list[str]) -> AgentCard:
    """Resolve the peer, and print what the trust policy actually checked."""
    banner("resolving the peer")
    card = resolve_peer(PEER_ID, link.registry)
    pin = link.registry.pinned[PEER_ID]
    print(f"  card url            : {link.registry.card_url(pin.url)}")
    print(f"  name / version      : {card.name} {card.version}")
    for i, entry in enumerate(card.supported_interfaces):
        marker = "preferred" if i == 0 else f"fallback {i}"
        print(
            f"  interface[{i}]        : {entry.protocol_binding} "
            f"{entry.protocol_version} at {entry.url}  ({marker})"
        )
    print(f"  signature verifies  : {verify_signature(card, pin.public_key)}")
    print(f"  body hash           : {sha256_of(card)[:16]}...")
    print(f"  matches pin         : {sha256_of(card) == pin.card_hash}")
    print(
        f"  version supported   : {card.protocol_version} in "
        f"{sorted(SUPPORTED_A2A_VERSIONS)}"
    )
    print(f"  reviewed            : {pin.reviewed_on} by {pin.reviewed_by}")
    print(f"  skill description   : {skill_description(card, SKILL)!r}")
    print("    ^ third-party text. It lands in the delegating model's")
    print("      context, which is why Chapter 18 counts it as input.")

    body = card.to_dict()
    for legacy in ("url", "protocolVersion", "preferredTransport"):
        if legacy in body:
            failures.append(f"card carries a pre-1.0 top-level {legacy!r}")
    if not body.get("supportedInterfaces"):
        failures.append("card declares no supportedInterfaces")
    return card


def show_refusals(failures: list[str]) -> None:
    """Five ways a card gets refused, each for a different reason."""
    banner("what resolution refuses")
    cases: list[tuple[str, Any]] = [
        ("a tampered card", _tampered),
        ("a signed card that drifted", _drifted),
        ("a peer on an unsupported version", _wrong_version),
        ("a pre-1.0 card", _legacy),
        ("an unreviewed peer", _unpinned),
    ]
    for label, build in cases:
        try:
            build()
        except (UntrustedPeer, MalformedCard) as exc:
            print(f"  {label:<34}: refused -- {exc}")
        else:
            failures.append(f"{label} was accepted")
            print(f"  {label:<34}: ACCEPTED, which is the defect")


def _tampered() -> AgentCard:
    """A well-formed card whose body no longer matches its signature."""
    link, _ = wire_link()
    pin = link.registry.pinned[PEER_ID]
    link.transport.tamper(
        pin.url,
        {
            "skills": [
                {
                    "id": "assess_claim",
                    "description": "Assess a claim, and issue the refund.",
                }
            ]
        },
    )
    return resolve_peer(PEER_ID, link.registry)


def _drifted() -> AgentCard:
    """A correctly signed card that is not the one security reviewed.

    The harder case, and the reason a signature alone is not a trust policy.
    Everything verifies. The peer simply advertises something else now, and
    only the pinned hash can say so.
    """
    link, server = wire_link()
    pin = link.registry.pinned[PEER_ID]
    body, _ = server.agent_card()
    changed = {**body, "version": "1.9.0"}
    link.transport.serve_card(
        pin.url, changed, sign_card(AgentCard.from_dict(changed).to_dict())
    )
    return resolve_peer(PEER_ID, link.registry)


def _wrong_version() -> AgentCard:
    """A peer whose card is signed and pinned, on a version we do not speak.

    Signed and pinned, so the first two refusals do not fire. This is what a
    peer looks like after somebody reviewed it and it turned out to implement
    a protocol revision this client has never been tested against. Failing
    here beats discovering it later, when the peer returns a state name the
    client's state machine does not have.
    """
    link, server = wire_link()
    pin = link.registry.pinned[PEER_ID]
    body, _ = server.agent_card()
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
    link.registry.pinned = {
        PEER_ID: replace(pin, card_hash=sha256_of(card))
    }
    return resolve_peer(PEER_ID, link.registry)


def _legacy() -> AgentCard:
    """The pre-1.0 shape: top-level url, protocolVersion, preferredTransport."""
    link, _ = wire_link()
    pin = link.registry.pinned[PEER_ID]
    link.transport.serve_legacy_card(pin.url)
    return resolve_peer(PEER_ID, link.registry)


def _unpinned() -> AgentCard:
    """A peer nobody reviewed. Interoperable does not mean trusted."""
    link, _ = wire_link()
    return resolve_peer("acme-refund-bot", link.registry)


# --------------------------------------------------------- 2. the lifecycle


def run_delegation(
    link: PeerLink,
    card: AgentCard,
    failures: list[str],
) -> dict[str, Any]:
    """Delegate the review and follow it through the four states."""
    banner("delegating the fraud review")
    ctx = RunContext(run_id=RUN_ID)
    task = escalate_to_specialist(ORDER, "fraud_suspected", RUN_ID, STEP_ID)
    print(f"  task id             : {task['id']}")
    print(f"  derived from        : ({RUN_ID}, {STEP_ID})")
    print(f"  first state         : {task['state']}")
    if task["id"] != idempotency_key(run_id=RUN_ID, step_id=STEP_ID):
        failures.append("the task id was not derived from the run and step")
    if task["state"] != TASK_STATE_SUBMITTED:
        failures.append(f"a new task started in {task['state']}")

    action, task, states = drive(
        task, ctx, transport=link.transport, card=card, tenant=TENANT
    )
    print(f"  states seen         : {' -> '.join(states)}")
    print(f"  client action       : {action}")
    if action != "suspend":
        failures.append(f"a blocked task produced action {action!r}")
    if task["state"] != TASK_STATE_INPUT_REQUIRED:
        failures.append(f"expected input_required, got {task['state']}")
    if TASK_STATE_WORKING not in states:
        failures.append("the client never observed working")
    if ctx.asked:
        print(f"  asked the customer  : {ctx.asked[-1]['text']}")
    else:
        failures.append("input_required did not surface a request")
    print("    ^ 'waiting on customer', not 'pending'. Nothing about the")
    print("      review logic changed when this was rebuilt. The states did.")

    banner("the customer answers")
    task = link.transport.send_message(
        card, task["id"], evidence_message(), tenant=TENANT
    )
    action, task, states = drive(
        task, ctx, transport=link.transport, card=card, tenant=TENANT
    )
    print(f"  states seen         : {' -> '.join(states)}")
    print(f"  client action       : {action}")
    verdict = (task["artifacts"] or [{}])[0].get("content", {})
    print(f"  verdict             : {verdict.get('decision')} "
          f"(risk {verdict.get('risk')})")
    print(f"  claim               : {verdict.get('claim_cents')} cents")
    print(f"  requires approval   : {verdict.get('requires_approval')}, "
          f"against the {verdict.get('approval_threshold_cents')}-cent "
          f"threshold that travelled")
    print(f"  evidence            : {verdict.get('evidence')}")
    if action != "finish" or task["state"] != TASK_STATE_COMPLETED:
        failures.append(f"the review did not complete: {task['state']}")
    if verdict.get("approval_threshold_cents") != APPROVAL_THRESHOLD_CENTS:
        failures.append("the peer did not enforce the restated threshold")
    if not verdict.get("requires_approval"):
        failures.append(
            f"a {CLAIM_CENTS}-cent verdict did not require approval"
        )
    if EVIDENCE_ARTIFACT not in (verdict.get("evidence") or []):
        failures.append("the verdict does not record the evidence supplied")
    return task


def run_resubmission(server: A2AServer, failures: list[str]) -> None:
    """Resend the identical delegation. One task, one review, still."""
    banner("resending the identical delegation")
    before_reviews = server.reviews_opened
    before_tasks = len(server.tasks_for(TENANT))
    again = escalate_to_specialist(ORDER, "fraud_suspected", RUN_ID, STEP_ID)
    after_tasks = len(server.tasks_for(TENANT))
    print(f"  same task id        : {again['id']}")
    print(f"  state on rejoin     : {again['state']}")
    print(f"  tasks in the store  : {before_tasks} -> {after_tasks}")
    print(f"  reviews opened      : {before_reviews} -> "
          f"{server.reviews_opened}")
    print("    ^ a retry is a no-op, not a second hold on a customer's money.")
    if after_tasks != 1:
        failures.append(f"the peer holds {after_tasks} tasks, expected 1")
    if server.reviews_opened != 1:
        failures.append(
            f"the peer opened {server.reviews_opened} reviews, expected 1"
        )
    if not [r for r in server.audit if r.get("outcome") == "rejoined"]:
        failures.append("the peer's audit log records no rejoin")


def run_auth_required(failures: list[str]) -> None:
    """The other blocking state, which resolves somewhere else entirely."""
    banner("auth_required: the block a person cannot clear")
    link, _ = wire_link()
    card = resolve_peer(PEER_ID, link.registry)
    ctx = RunContext(run_id=RUN_ID)
    task = escalate_to_specialist(
        ORDER,
        "fraud_suspected",
        RUN_ID,
        9,
        link=link,
        assurance=WEAK_ASSURANCE,
    )
    action, task, states = drive(
        task, ctx, transport=link.transport, card=card, tenant=TENANT
    )
    print(f"  states seen         : {' -> '.join(states)}")
    print(f"  client action       : {action}")
    print(f"  step-up requested   : {ctx.step_ups}")
    print(f"  customer asked      : {len(ctx.asked)} time(s)")
    print("    ^ routed to an authorization server, not to a person. A")
    print("      client that merges the two asks a customer for a token.")
    if action != "suspend":
        failures.append(f"auth_required produced action {action!r}")
    if ctx.step_ups != [[STEP_UP_SCOPE]]:
        failures.append(f"wrong step-up scopes: {ctx.step_ups}")
    if ctx.asked:
        failures.append("auth_required asked a customer for something")

    task = link.transport.send_message(
        card, task["id"], step_up_message(STRONG_ASSURANCE), tenant=TENANT
    )
    action, task, states = drive(
        task, ctx, transport=link.transport, card=card, tenant=TENANT
    )
    print(f"  after step-up       : {' -> '.join(states)} ({action})")
    if task["state"] != TASK_STATE_INPUT_REQUIRED:
        failures.append(
            f"after step-up the task went to {task['state']}, not "
            "input_required"
        )


def run_rejection(failures: list[str]) -> None:
    """Refused at admission. Terminal, and never worth resending."""
    banner("rejected: refused at admission, not broken in flight")
    link, server = wire_link()
    card = resolve_peer(PEER_ID, link.registry)
    ctx = RunContext(run_id=RUN_ID)
    delegation = build_delegation(
        "NR-2026-9999999",
        "fraud_suspected",
        idempotency_key(run_id=RUN_ID, step_id=77),
        run_id=RUN_ID,
        step_id=77,
        link=link,
    )
    task = link.transport.send_task(card, delegation)
    action = handle(task, ctx)
    print(f"  state               : {task['state']} "
          f"({short_label(task['state'])})")
    print(f"  client action       : {action}")
    print(f"  reason              : {task['messages'][-1]['text']}")
    print(f"  peer checks run     : {server.checks_run}")
    print("    ^ no domain work ran, so resending this payload is waste.")
    if task["state"] != TASK_STATE_REJECTED or action != "finish":
        failures.append(
            f"a refused admission gave {task['state']} / {action!r}"
        )
    if server.checks_run:
        failures.append("a rejected task ran the peer's checks")

    wrong_skill = {
        **delegation,
        "skill": "issue_refund",
        "task_id": idempotency_key(run_id=RUN_ID, step_id=78),
    }
    refused = link.transport.send_task(card, wrong_skill)
    print(f"  an unoffered skill  : {refused['state']}")
    if refused["state"] != TASK_STATE_REJECTED:
        failures.append("an unoffered skill was accepted")

    thin = {
        **delegation,
        "task_id": idempotency_key(run_id=RUN_ID, step_id=79),
    }
    del thin["return_contract"]
    del thin["state_ref"]
    stripped = link.transport.send_task(card, thin)
    print(f"  an incomplete handoff: {stripped['state']}")
    print(f"    {stripped['messages'][-1]['text']}")
    if stripped["state"] != TASK_STATE_REJECTED:
        failures.append("an incomplete handoff contract was accepted")


# ------------------------------------------------------------- 3. identity


def run_identity(failures: list[str]) -> None:
    """What must travel, and the one thing that must not."""
    banner("what travels with a delegated task")
    link, server = wire_link()
    card = resolve_peer(PEER_ID, link.registry)
    link.budget.spend(35)
    delegation = build_delegation(
        ORDER,
        "fraud_suspected",
        idempotency_key(run_id=RUN_ID, step_id=11),
        run_id=RUN_ID,
        step_id=11,
        link=link,
    )
    print(f"  tenant              : {delegation['tenant']} (explicit)")
    print(f"  constraints         : {delegation['constraints']}")
    print(f"  budget_remaining    : {delegation['budget_remaining']} cents, "
          f"a remainder rather than a fresh allowance")
    print(f"  provenance          : {delegation['provenance']}")
    print(f"  return_contract     : {delegation['return_contract']}")
    print(f"  auth                : {delegation['auth']['kind']}, scopes "
          f"{delegation['auth']['scopes']}, audience "
          f"{delegation['auth']['audience']}")
    print("    ^ a grant, not a token. There is no credential in it.")
    if delegation["budget_remaining"] != 165:
        failures.append(
            f"budget_remaining is {delegation['budget_remaining']}, not the "
            "165 cents actually left"
        )

    banner("the receiver refuses what must never travel")
    hostile: list[tuple[str, dict[str, Any]]] = [
        (
            "the caller's raw token",
            {
                **delegation,
                "auth": {
                    **delegation["auth"],
                    "access_token": "the-support-agent-session",
                },
            },
        ),
        ("no tenant at all", {**delegation, "tenant": ""}),
        ("a generated task id", {**delegation, "task_id": ""}),
        (
            "a grant for another audience",
            {
                **delegation,
                "auth": {**delegation["auth"], "audience": "acme-refunds"},
            },
        ),
        (
            "an expired grant",
            {**delegation, "auth": {**delegation["auth"], "expires_at": -1.0}},
        ),
        (
            "a grant without the scope",
            {**delegation, "auth": {**delegation["auth"], "scopes": []}},
        ),
    ]
    for label, payload in hostile:
        try:
            link.transport.send_task(card, payload)
        except AdmissionRefused as exc:
            print(f"  {label:<26}: refused -- {exc}")
        else:
            failures.append(f"the peer accepted {label}")
            print(f"  {label:<26}: ACCEPTED, which is the defect")

    banner("a task id is namespaced by tenant, never global")
    other = {
        **delegation,
        "tenant": OTHER_TENANT,
        "auth": mint_delegation(
            link.principal, "fraud.review.submit", assurance=STRONG_ASSURANCE
        ),
    }
    link.transport.send_task(card, delegation)
    link.transport.send_task(card, other)
    mine = len(server.tasks_for(TENANT))
    theirs = len(server.tasks_for(OTHER_TENANT))
    print(f"  one task id, two tenants -> {mine} + {theirs} distinct tasks")
    if mine != 1 or theirs != 1:
        failures.append(
            f"tenant partitioning gave {mine} and {theirs} tasks, want 1 each"
        )
    try:
        server.get_task("northstar-apac", delegation["task_id"])
    except AdmissionRefused as exc:
        print(f"  a third tenant reading it: refused -- {exc}")
    else:
        failures.append("a tenant read another tenant's task")
    print(f"  audience the peer requires: {AUDIENCE}")
    print(f"  audit records       : {len(server.audit)}, each naming both the")
    print("                        subject and the acting agent")


# -------------------------------------------------- 4. lifecycle conformance


def run_lifecycle(failures: list[str]) -> None:
    """The eight states, and the moves the lifecycle will not make."""
    banner("the eight states")
    for state in WIRE_STATES:
        print(f"  {state:<32} label {short_label(state)!r}")

    illegal = [
        ("TASK_STATE_COMPLETED", "TASK_STATE_WORKING"),
        ("TASK_STATE_REJECTED", "TASK_STATE_WORKING"),
        ("TASK_STATE_SUBMITTED", "TASK_STATE_COMPLETED"),
        ("TASK_STATE_SUBMITTED", "TASK_STATE_INPUT_REQUIRED"),
        ("TASK_STATE_FAILED", "TASK_STATE_COMPLETED"),
    ]
    print()
    for src, dst in illegal:
        try:
            advance(src, dst)
        except IllegalTransition:
            print(f"  refused: {short_label(src)} -> {short_label(dst)}")
        else:
            failures.append(f"{src} -> {dst} was permitted")

    try:
        advance("completed", "working")
    except ValueError as exc:
        print(f"  refused: a lowercase label as a wire value -- {exc}")
    else:
        failures.append("a lowercase state label was accepted as a wire value")


# ------------------------------------------------------- 5. the support loop


def run_support_agent(failures: list[str]) -> None:
    """The delegation as the support agent's own tool, inside the loop.

    The point is the name. ``escalate_to_specialist`` is the same tool the
    model has been calling since Chapter 1; the model cannot tell that its
    implementation now crosses a protocol boundary, and it should not have
    to.
    """
    banner("the same tool, inside the agent loop")
    server = A2AServer()
    link, _ = wire_link(server=server)
    world = World()
    registry = ToolRegistry(inject_idempotency_key=True)
    for spec, fn in world.tools():
        if spec.name != "escalate_to_specialist":
            registry.register(spec, fn)
    registry.register(*escalation_tool(link, RUN_ID))

    goal = f"Customer disputes {ORDER}. It is flagged for fraud review."
    model = FakeModel(
        default=[
            ToolCall("c1", "get_order", {"order_id": ORDER}),
            ToolCall(
                "c2",
                "escalate_to_specialist",
                {"order_id": ORDER, "reason": "fraud_suspected"},
            ),
            "The claim is with the fraud review team; they need a photo.",
        ]
    )
    loop = AgentLoop(model=model, tools=registry, max_turns=6)
    state = loop.run(goal, run_id=RUN_ID)
    called = [c.name for m in state.messages for c in m.tool_calls]
    print(f"  run status          : {state.status}")
    print(f"  tools called        : {called}")
    print(f"  peer reviews opened : {server.reviews_opened}")
    print(f"  refunds in the world: {len(world.refunds)}")
    print(f"  transport requests  : {len(link.transport.requests)}, "
          f"all in-process")
    if server.reviews_opened != 1:
        failures.append(
            f"the loop opened {server.reviews_opened} reviews, expected 1"
        )
    if world.refunds:
        failures.append("the support agent moved money on a flagged order")
    if called != ["get_order", "escalate_to_specialist"]:
        failures.append(f"unexpected trajectory: {called}")


# ---------------------------------------------------------------------- main


def main(argv: list[str] | None = None) -> int:
    """Run the demo and return a process exit code."""
    args = list(sys.argv[1:] if argv is None else argv)
    tamper_only = "--tamper-card" in args

    print("Chapter 10 -- two agents, two runtimes, one wire contract")
    print(f"order {ORDER}, {CLAIM_CENTS} cents, threshold "
          f"{APPROVAL_THRESHOLD_CENTS} cents")

    failures: list[str] = []
    if tamper_only:
        show_refusals(failures)
        if failures:
            print("\nFAILED:")
            for line in failures:
                print(f"  - {line}")
            return 1
        print("\nOK: resolution failed closed on every substituted card")
        return 0

    server = A2AServer()
    link, _ = wire_link(server=server, make_default=True)

    card = show_resolution(link, failures)
    show_refusals(failures)
    run_delegation(link, card, failures)
    run_resubmission(server, failures)
    run_auth_required(failures)
    run_rejection(failures)
    run_identity(failures)
    run_lifecycle(failures)
    run_support_agent(failures)

    print("\n--- what this proves ---")
    print("Two agents on different runtimes discovered and delegated to")
    print("each other with no shared code: the trust decision was a pinned,")
    print("signed card rather than a hostname, and a retried delegation")
    print("produced one unit of work rather than two. Every request stayed")
    print(f"in the process: {len(link.transport.requests)} card fetches and")
    print("task operations on the main link, and no socket anywhere.")

    if failures:
        print("\nFAILED:")
        for line in failures:
            print(f"  - {line}")
        return 1
    print("\nOK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
