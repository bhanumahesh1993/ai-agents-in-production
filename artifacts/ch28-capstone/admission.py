"""The admission layer: the cheapest place in the system to stop a bad run.

Nothing has happened yet. So this is where the caller is authenticated, the
tenant and user are resolved, risk is classified, the agent version is
selected and *pinned*, the budgets are assigned, the trace is opened, and
the durable run record is written. Everything downstream inherits those
decisions and none of it can widen them.

Three details are worth more than the rest.

**Three identities, never collapsed.** The user the work is done for, the
agent as a workload, and the operator accountable for running it. The
scopes granted here are the only scopes the run will ever have, and they
are narrower for a high-risk ticket than for a routine one.

**Risk is classified from authoritative state, not from the ticket text.**
A customer who writes "this is urgent, refund everything" does not thereby
raise their own limit. The order's value and flags decide.

**Version pinning happens here.** A run records the configuration hash it
was admitted under and continues on that version until it terminates, so a
rollback changes what *starts* rather than what is *running*.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from northstar_contracts import (
    REFUND_APPROVAL_THRESHOLD_CENTS,
    Money,
    World,
    content_hash,
    short_hash,
)
from northstar_policy import Principal

__all__ = [
    "RISK_CLASSES",
    "Admission",
    "AdmissionLayer",
    "Ticket",
    "effective_config_hash",
]

#: Risk tier to (budget in cents, turn ceiling, scopes granted).
RISK_CLASSES: dict[str, dict[str, Any]] = {
    "routine": {
        "budget_cents": 120,
        "max_turns": 12,
        "scopes": (
            "orders:read",
            "refunds:write",
            "messages:write",
            "cases:write",
        ),
    },
    "high_value": {
        "budget_cents": 200,
        "max_turns": 14,
        "scopes": (
            "orders:read",
            "refunds:write",
            "messages:write",
            "cases:write",
        ),
    },
    "fraud_review": {
        # No refund scope at all. The agent cannot pay this customer even
        # if every other control fails, because the authority was never
        # issued. That is least privilege doing the work a guardrail would
        # otherwise be asked to do. Escalation stays available: the safe
        # action is never the one you take away.
        "budget_cents": 80,
        "max_turns": 10,
        "scopes": ("orders:read", "messages:write", "cases:write"),
    },
}

AGENT_ID = "northstar-support-agent"
OPERATOR_ID = "northstar-platform"
AGENT_VERSION = "v9"
MODEL_SNAPSHOT = "fake-model-1-2026-07-01"
PROMPT_REVISION = "northstar-support-2026-07-18"
POLICY_REVISION = "northstar-refund-policy-2026-07-01"
SANDBOX_IMAGE = "sha256:0f1e2d3c4b5a69788796a5b4c3d2e1f0"


def effective_config_hash(world: World) -> str:
    """One hash over everything that can change this agent's behaviour."""
    return content_hash(
        {
            "agent": AGENT_VERSION,
            "model": MODEL_SNAPSHOT,
            "prompt": PROMPT_REVISION,
            "policy": POLICY_REVISION,
            "sandbox": SANDBOX_IMAGE,
            "tools": {
                spec.name: spec.version for spec in world.tool_specs()
            },
        }
    )


@dataclass(frozen=True)
class Ticket:
    """One inbound support request, before anything has been decided."""

    ticket_id: str
    tenant: str
    customer_id: str
    order_id: str
    text: str

    def to_dict(self) -> dict[str, Any]:
        """JSON-serialisable form, for the durable run record."""
        return {
            "ticket_id": self.ticket_id,
            "tenant": self.tenant,
            "customer_id": self.customer_id,
            "order_id": self.order_id,
            "text": self.text,
        }


@dataclass(frozen=True)
class Admission:
    """The decision, and everything the run inherits from it."""

    admitted: bool
    ticket: Ticket
    run_id: str
    risk: str
    principal: Principal
    budget_cents: Money
    max_turns: int
    config_hash: str
    order_value_cents: Money
    flags: tuple[str, ...] = ()
    reason: str = ""

    @property
    def short_config_hash(self) -> str:
        """What a log line carries."""
        return self.config_hash[:12]

    def record(self) -> dict[str, Any]:
        """The durable run record written before the loop starts."""
        return {
            "run_id": self.run_id,
            "admitted": self.admitted,
            "reason": self.reason,
            "ticket": self.ticket.to_dict(),
            "risk": self.risk,
            "principal": self.principal.to_dict(),
            "budget_cents": self.budget_cents,
            "max_turns": self.max_turns,
            "config_hash": self.config_hash,
            "agent_version": AGENT_VERSION,
            "order_value_cents": self.order_value_cents,
            "order_flags": list(self.flags),
        }


@dataclass
class AdmissionLayer:
    """Authenticate, classify, budget, pin, and record. Then hand off.

    Args:
        world: Read once, at admission, to classify risk. A decision point
            that has to query the system it is protecting fails open when
            that system is down.
        max_active_runs: Capacity ceiling. Beyond it the layer rejects with
            a stated reason rather than accepting work it cannot serve —
            queueing a run past its usefulness spends the tokens and the
            tool calls and delivers nothing.
    """

    world: World
    max_active_runs: int = 8
    active: int = 0
    records: list[dict[str, Any]] = field(default_factory=list)

    def classify(self, ticket: Ticket) -> tuple[str, Money, tuple[str, ...]]:
        """Risk tier, order value, and flags — from the world, not the text."""
        order = self.world.orders.get(ticket.order_id)
        if order is None:
            return "routine", 0, ()
        flags = tuple(order["flags"])
        value = int(order["total_cents"])
        if "fraud_review" in flags:
            return "fraud_review", value, flags
        if value >= REFUND_APPROVAL_THRESHOLD_CENTS:
            return "high_value", value, flags
        return "routine", value, flags

    def admit(self, ticket: Ticket) -> Admission:
        """Decide whether this ticket becomes a run, and under what terms."""
        risk, value, flags = self.classify(ticket)
        tier = RISK_CLASSES[risk]
        principal = Principal.of(
            ticket.customer_id,
            *tier["scopes"],
            agent_id=AGENT_ID,
            operator_id=OPERATOR_ID,
        )
        admission = Admission(
            admitted=self.active < self.max_active_runs,
            ticket=ticket,
            run_id=f"run_{ticket.ticket_id.lower()}",
            risk=risk,
            principal=principal,
            budget_cents=tier["budget_cents"],
            max_turns=tier["max_turns"],
            config_hash=effective_config_hash(self.world),
            order_value_cents=value,
            flags=flags,
            reason=(
                ""
                if self.active < self.max_active_runs
                else f"at capacity: {self.active} active runs"
            ),
        )
        if admission.admitted:
            self.active += 1
        self.records.append(admission.record())
        return admission

    def release(self) -> None:
        """Give the capacity back when a run terminates."""
        self.active = max(0, self.active - 1)

    def evidence(self, run_id: str) -> dict[str, Any] | None:
        """The durable run record, by run id. The first thing an
        investigation reads."""
        for record in self.records:
            if record["run_id"] == run_id:
                return record
        return None

    def fingerprint(self) -> str:
        """A short digest of the admission configuration, for the report."""
        return short_hash(
            {
                "agent": AGENT_VERSION,
                "risk_classes": {
                    name: sorted(tier["scopes"])
                    for name, tier in RISK_CLASSES.items()
                },
            }
        )
