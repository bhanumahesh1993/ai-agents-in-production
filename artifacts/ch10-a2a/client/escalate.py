"""``escalate_to_specialist``, unchanged in name, now crossing a boundary.

The tool has had this name and this job since Chapter 1. For its first year
it was a function call, and it should have been: A2A adds a network hop, a
serialization format, an authentication story, a lifecycle state machine,
and a second thing to operate, and none of that is worth paying for inside
one process. It became a delegation the day the fraud review agent shipped
on its own release train.

Two properties of this module carry the chapter.

**The task id is derived, never generated.** ``idempotency_key(run_id,
step_id)`` is the same function that keys ``issue_refund``, for the same
reason. A random task id per attempt is a nonce: the retry presents a new
identity for the same intent, and for Northstar a duplicate fraud review is
a second hold on a customer's refund while the first one is still open.

**The delegation carries a grant, not a credential.** ``auth`` is a scoped,
short-lived delegation minted for this one call, which the receiver
exchanges under its own identity. What must never travel is the caller's
raw token: that makes the receiver a confused deputy holding the sender's
full authority, and it destroys the audit trail, because every downstream
action then appears under a single principal with no record of who asked.

All six fields of the Chapter 6 handoff contract travel, restated. In
process they were good practice. Across a boundary the receiver has no other
way to learn any of them, which is why
:meth:`peer.adapter.A2AServer._decline` rejects a payload missing any one.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from northstar_contracts import (
    Money,
    ToolSpec,
    idempotency_key,
    short_hash,
)
from northstar_policy import BudgetGuard, Principal
from transport import MockTransport
from wire import APPROVAL_THRESHOLD_CENTS, HANDOFF_FIELDS

from client.resolve import PEER_ID, PeerRegistry, resolve_peer

__all__ = [
    "DELEGATION_TTL_SECONDS",
    "REQUIRED_SCOPE",
    "SKILL",
    "STRONG_ASSURANCE",
    "WEAK_ASSURANCE",
    "Delegator",
    "PeerLink",
    "RunBudget",
    "ESCALATE_SPEC",
    "build_delegation",
    "default_link",
    "set_default_link",
    "escalate_to_specialist",
    "escalation_tool",
    "mint_delegation",
    "reset_default_link",
    "default_principal",
    "handoff_fields_present",
]

#: The skill this client invokes on the peer.
SKILL = "assess_claim"

#: The scope the delegation is minted for. Narrow: it authorizes submitting
#: one fraud review, and nothing else the peer can do.
REQUIRED_SCOPE = "fraud.review.submit"

#: Grants are short-lived because a long-lived one is a credential.
DELEGATION_TTL_SECONDS = 60.0

#: Assurance levels. The peer requires the strong one for a large claim and
#: stops in ``auth_required`` when it does not get it.
STRONG_ASSURANCE = "mfa"
WEAK_ASSURANCE = "password"


@dataclass(frozen=True)
class Delegator:
    """Who is delegating: the book's :class:`Principal`, plus a tenant.

    The tenant is a separate field on purpose. It travels explicitly in
    every delegation, and the receiver reads it from the payload, because a
    receiver that infers the tenant from the credential loses every tenant
    boundary the first day a shared service identity appears.
    """

    principal: Principal
    tenant_id: str

    @property
    def scopes(self) -> frozenset[str]:
        """Scopes granted for this run."""
        return self.principal.scopes


class RunBudget:
    """The run's remaining spend, as a remainder rather than an allowance.

    Wraps :class:`northstar_policy.budget.BudgetGuard` and adds one method,
    because what crosses a boundary is what is *left*. Send a fresh
    allowance instead and a three-hop chain spends three full budgets while
    every agent in it truthfully reports staying within limits.
    """

    def __init__(self, guard: BudgetGuard | None = None) -> None:
        self.guard = guard or BudgetGuard(max_cents=200, max_turns=12)

    def remainder(self) -> Money:
        """Cents left in this run's budget. Never the original limit."""
        left = self.guard.remaining_cents
        return 0 if left is None else int(left)

    def spend(self, cents: Money) -> Money:
        """Charge the run, and return what is left."""
        self.guard.charge(cents)
        return self.remainder()


@dataclass
class PeerLink:
    """Everything a delegation needs, in one object a test can build fresh.

    The chapter's excerpt reads module-level ``REGISTRY``, ``PRINCIPAL``, and
    ``BUDGET``, which is how this looks in a service that wires itself once
    at startup. Holding them on a link instead means a test gets its own
    transport, its own peer, and its own budget per test, so no test can see
    another test's tasks.
    """

    transport: MockTransport
    registry: PeerRegistry
    principal: Delegator
    budget: RunBudget


def mint_delegation(
    principal: Delegator,
    *scopes: str,
    assurance: str = STRONG_ASSURANCE,
    now: float = 0.0,
    ttl_seconds: float = DELEGATION_TTL_SECONDS,
) -> dict[str, Any]:
    """Mint a scoped, short-lived grant for one call.

    Args:
        principal: Who is delegating.
        scopes: What the receiver may do. Narrow this to the one skill.
        assurance: How the caller authenticated. The peer requires a
            step-up for a large claim, and says so with ``auth_required``
            rather than failing or quietly proceeding with more privilege
            than the task warranted.
        now: Injected clock, so nothing in the suite sleeps.
        ttl_seconds: Lifetime.

    Returns:
        A grant naming the subject, the actor, the audience, the scopes, and
        the chain of agents traversed so far. There is no token in it, and
        :data:`peer.adapter.RAW_TOKEN_KEYS` is how the receiver checks.
    """
    return {
        "kind": "delegation",
        "subject": principal.principal.user_id,
        "actor": principal.principal.agent_id,
        "audience": PEER_ID,
        "scopes": sorted(scopes),
        "assurance": assurance,
        "issued_at": now,
        "expires_at": now + ttl_seconds,
        "chain": [
            principal.principal.operator_id,
            principal.principal.agent_id,
        ],
    }


def build_delegation(
    order_id: str,
    reason: str,
    task_id: str,
    *,
    run_id: str,
    step_id: str | int,
    link: PeerLink,
    assurance: str = STRONG_ASSURANCE,
    now: float = 0.0,
) -> dict[str, Any]:
    """The payload, with all six handoff fields restated.

    Args:
        order_id: The order under review.
        reason: Why it was escalated.
        task_id: Derived by :func:`escalate_to_specialist`.
        run_id: The originating run.
        step_id: The originating step.
        link: Transport, registry, principal, and budget.
        assurance: Assurance level for the minted grant.
        now: Injected clock.

    Returns:
        The delegation. Every one of :data:`wire.HANDOFF_FIELDS` is present,
        and the receiver rejects the task if any is not.
    """
    return {
        "task_id": task_id,                 # derived, so a retry rejoins
        "skill": SKILL,
        "tenant": link.principal.tenant_id,  # explicit, never inferred
        "goal": f"Assess refund claim for {order_id}: {reason}",
        "constraints": {
            "approval_threshold_cents": APPROVAL_THRESHOLD_CENTS,
            "reason": reason,
            "may_move_money": False,
        },
        "state_ref": {
            "order_id": order_id,
            "held_by": "northstar-orders",
        },
        "budget_remaining": link.budget.remainder(),
        "provenance": {
            "run_id": run_id,
            "step_id": str(step_id),
            "trace_parent": short_hash({"run": run_id, "step": step_id}),
            "agent_chain": [link.principal.principal.agent_id],
        },
        "return_contract": {
            "artifact": "fraud_verdict",
            "modes": ["application/json"],
        },
        "auth": mint_delegation(
            link.principal,
            REQUIRED_SCOPE,
            assurance=assurance,
            now=now,
        ),
    }


def escalate_to_specialist(
    order_id: str,
    reason: str,
    run_id: str,
    step_id: str | int,
    *,
    link: PeerLink | None = None,
    assurance: str = STRONG_ASSURANCE,
    now: float = 0.0,
) -> dict[str, Any]:
    """Delegate a fraud review. Same task_id on retry: no duplicate.

    Args:
        order_id: The order under review.
        reason: Why it was escalated.
        run_id: The originating run. Half of the task id.
        step_id: The originating step. The other half.
        link: Wiring. Defaults to :func:`default_link`, which is how a
            service that wires itself at startup looks.
        assurance: Assurance level for the minted grant. Pass
            :data:`WEAK_ASSURANCE` to see the peer stop in
            ``auth_required``.
        now: Injected clock.

    Returns:
        The task, in ProtoJSON form, at whichever state the peer stopped in.
        Route it through :func:`client.follow.handle`; do not assume it is
        ``submitted``.

    Raises:
        client.resolve.UntrustedPeer: If the peer's card does not resolve.
        peer.adapter.AdmissionRefused: If the peer refuses the identity.
    """
    link = link or default_link()
    card = resolve_peer(PEER_ID, link.registry)
    task_id = idempotency_key(run_id=run_id, step_id=step_id)
    delegation = build_delegation(
        order_id,
        reason,
        task_id,
        run_id=run_id,
        step_id=step_id,
        link=link,
        assurance=assurance,
        now=now,
    )
    return link.transport.send_task(card, delegation)


# ------------------------------------------------------- the loop's tool


#: The contract the support agent's model sees. ``run_id`` and ``step_id``
#: are absent from the schema on purpose: they are the run's identity, not
#: the model's to choose. The registry stamps ``idempotency_key`` from
#: ``(run_id, step, call_id)`` and that stamp *is* the step identifier the
#: task id is derived from, so a replayed step rejoins its own task.
ESCALATE_SPEC = ToolSpec(
    name="escalate_to_specialist",
    description=(
        "Hand the case to the Northstar fraud review agent, which runs as "
        "a separate service and may take minutes or hours. Use this for "
        "anything flagged fraud_review and whenever you are not confident "
        "a refund is legitimate. Returns a task with a state, not a "
        "verdict: the task may come back waiting on the customer for "
        "evidence. Escalating the same case twice rejoins the open task "
        "and does not open a second review. Does not move money and does "
        "not message the customer."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "order_id": {
                "type": "string",
                "pattern": "^NR-[0-9]{4}-[0-9]{7}$",
            },
            "reason": {
                "type": "string",
                "enum": [
                    "fraud_suspected",
                    "damaged",
                    "not_delivered",
                    "changed_mind",
                ],
            },
            "idempotency_key": {
                "type": "string",
                "description": (
                    "Stable key for this intent. The task id is derived "
                    "from it, so a retry rejoins the existing review."
                ),
            },
        },
        "required": ["order_id", "reason"],
        "additionalProperties": False,
    },
    output_schema={
        "type": "object",
        "properties": {
            "id": {"type": "string"},
            "state": {"type": "string"},
            "skill": {"type": "string"},
            "messages": {"type": "array"},
        },
        "required": [],
        "additionalProperties": False,
    },
    writes=True,
    idempotent=True,
    max_result_tokens=400,
    version="2",
)


def escalation_tool(
    link: PeerLink,
    run_id: str,
    *,
    assurance: str = STRONG_ASSURANCE,
) -> tuple[ToolSpec, Callable[..., dict[str, Any]]]:
    """The delegation, registered as the support agent's tool.

    Args:
        link: Wiring for this run.
        run_id: The run the tool is bound to.
        assurance: Assurance level for grants minted by this binding.

    Returns:
        A ``(spec, fn)`` pair for
        :meth:`northstar_runtime.registry.ToolRegistry.register`. Build the
        registry with ``inject_idempotency_key=True`` so the stamp arrives.
    """

    def escalate(
        order_id: str,
        reason: str,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """Delegate over A2A, using the registry's stamp as the step id."""
        return escalate_to_specialist(
            order_id,
            reason,
            run_id,
            idempotency_key or "unstamped",
            link=link,
            assurance=assurance,
        )

    return ESCALATE_SPEC, escalate


# ----------------------------------------------------- the startup wiring

#: Installed by :func:`wiring.wire_link`. Module-level, because that is what a
#: service wiring itself once at startup looks like, and it is what the
#: chapter's excerpt reads when it says ``REGISTRY`` and ``PRINCIPAL``.
_DEFAULT_LINK: PeerLink | None = None


def default_principal() -> Delegator:
    """The support agent, acting for one customer, in one tenant."""
    return Delegator(
        principal=Principal.of(
            "CUST-9032",
            "orders:read",
            REQUIRED_SCOPE,
            agent_id="northstar-support-agent",
            operator_id="northstar-platform",
        ),
        tenant_id="northstar-us",
    )


def default_link() -> PeerLink:
    """The link a bare :func:`escalate_to_specialist` uses.

    Installed once, by :func:`wiring.wire_link` at startup. This function does
    not build one on demand, which is deliberate: the client cannot wire
    itself without knowing where the peer's code lives, and it must not know
    that. A service that has not run its startup wiring cannot delegate, and
    saying so here beats discovering it at the first refund.

    Raises:
        RuntimeError: If nothing has been wired yet.
    """
    if _DEFAULT_LINK is None:
        raise RuntimeError(
            "no peer link is wired. Call wiring.wire_link() at startup, or "
            "pass link=... to escalate_to_specialist."
        )
    return _DEFAULT_LINK


def reset_default_link() -> None:
    """Forget the cached link. Called between tests."""
    global _DEFAULT_LINK
    _DEFAULT_LINK = None


def set_default_link(link: PeerLink) -> None:
    """Install the link a bare :func:`escalate_to_specialist` will use.

    What a service's startup does once. The demo calls it so that the
    chapter's four-argument call shape and the wiring it asserts against are
    the same objects.
    """
    global _DEFAULT_LINK
    _DEFAULT_LINK = link


def handoff_fields_present(delegation: dict[str, Any]) -> list[str]:
    """Which of the six handoff fields are missing. Empty is correct."""
    return [f for f in HANDOFF_FIELDS if f not in delegation]
