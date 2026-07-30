"""The orchestrator: three isolated readers, one component that may write.

The reconciler here is the best-informed component in the system rather
than the worst. It holds the continuous history that produced the plan plus
every finding, which is more context than any worker had, and it is the only
identity with a scope that can move money.

Two shapes live in this module. :func:`research` is the read-heavy fan-out:
three workers, three questions, one crossing each, in parallel, never again.
:func:`resolve_ticket` is the same ticket the broken demo hands to two
writers — but the two candidate resolutions come back as findings rather
than as actions, and the lead picks one before anything is written.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import subagent
from northstar_contracts import (
    Message,
    Money,
    RunState,
    ToolCall,
    World,
    estimate_tokens,
)
from northstar_policy import Principal, default_northstar_policy
from northstar_runtime import AgentLoop, FakeModel, ToolRegistry
from subagent import Finding, spawn_reader

__all__ = [
    "ADVISORY_QUESTIONS",
    "LEAD_BUDGET_CENTS",
    "ORDER_ID",
    "QUESTIONS",
    "REFUND_CENTS",
    "RESEARCH_GOAL",
    "Research",
    "all_tools",
    "lead_principal",
    "research",
    "resolve_ticket",
]

ORDER_ID = "NR-2026-0041827"
REFUND_CENTS: Money = 3250
LEAD_BUDGET_CENTS: Money = 200

QUESTIONS = [
    "Which June orders carry more than one refund event?",
    "What does policy allow for a duplicate refund, by sku?",
    "Which of those orders still have an open ticket?",
]

RESEARCH_GOAL = (
    "Reconcile the June refund history for CUST-8841 and say whether any "
    "order was paid twice."
)

#: The two resolutions the broken demo lets two writers settle independently.
#: Asked as questions, they come back as evidence and the lead decides.
ADVISORY_QUESTIONS = [
    "Refund option: is the cracked lamp shade refundable, and for how much?",
    "Replacement option: is the lamp shade in stock for a reshipment?",
]


def lead_principal() -> Principal:
    """The only identity in this artifact that can move money."""
    return Principal.of(
        "CUST-8841", "orders:read", "policy:read", "refunds:write"
    )


def all_tools(world: World) -> ToolRegistry:
    """Reads and writes, with every write stamped with a derived key.

    ``inject_idempotency_key`` is on here and it is not what makes the
    broken demo broken. Both writers in ``parallel_writers.py`` get keys
    too. A key makes one intent safe to repeat; it has nothing to say about
    two different intents that should never both have happened.
    """
    return ToolRegistry(inject_idempotency_key=True).register_all(
        world.tools()
    )


@dataclass(frozen=True)
class Research:
    """What one orchestrated research run cost and concluded."""

    state: RunState
    findings: tuple[Finding, ...]

    @property
    def worker_tokens(self) -> int:
        """Everything the three workers' own contexts held."""
        return sum(f.worker_tokens for f in self.findings)

    @property
    def intake_tokens(self) -> int:
        """What actually crossed into the orchestrator's window."""
        return sum(f.tokens for f in self.findings)

    @property
    def compression(self) -> float:
        """Worker context divided by orchestrator intake."""
        return self.worker_tokens / max(1, self.intake_tokens)

    @property
    def lead_tokens(self) -> int:
        """The orchestrator's whole context at the end of the run."""
        return estimate_tokens([m.content for m in self.state.messages])


#: The lead's trajectory: confirm the order it is about to speak about, then
#: answer. It never re-reads what the workers read.
LEAD_SCRIPT: dict[str, list[Any]] = {
    "Reconcile the June refund history": [
        ToolCall("l1", "get_order", {"order_id": ORDER_ID}),
        "No June order is currently double-paid. NR-2026-0041827 is the "
        "order whose July retry produced the incident, and policy allows "
        "one refund per claim, so any second row is a defect rather than "
        "an entitlement.",
    ],
}


def research(world: World, goal: str = RESEARCH_GOAL) -> Research:
    """Fan out three isolated readers, then reconcile in one place."""
    findings = [spawn_reader(world, q) for q in QUESTIONS]
    lead = AgentLoop(
        model=FakeModel(LEAD_SCRIPT),
        tools=all_tools(world),          # reads + writes
        policy=default_northstar_policy(),
        max_turns=12,
        budget_cents=LEAD_BUDGET_CENTS,
        principal=lead_principal(),      # readers hold no scopes at all
    )
    state = lead.start(goal, run_id="run_ch05_lead")
    for f in findings:
        state = state.with_messages(Message(role="user", content=f.summary))
    return Research(state=lead.resume(state), findings=tuple(findings))


#: One script per advisory question, and the lead's script for act three.
ADVISOR_SCRIPTS: dict[str, list[Any]] = {
    "Refund option": [
        ToolCall("a1", "get_order", {"order_id": ORDER_ID}),
        ToolCall("a2", "get_policy", {"reason": "damaged"}, ),
        "Refundable in full at 3250 cents, below the 5000-cent threshold.",
    ],
    "Replacement option": [
        ToolCall("b1", "get_order", {"order_id": ORDER_ID}),
        "A replacement shade is in stock and could ship today.",
    ],
}

RESOLVE_SCRIPT: dict[str, list[Any]] = {
    "Make ticket 8812 right": [
        ToolCall(
            "d1",
            "issue_refund",
            {
                "order_id": ORDER_ID,
                "amount_cents": REFUND_CENTS,
                "reason": "damaged",
            },
        ),
        ToolCall(
            "d2",
            "send_message",
            {
                "order_id": ORDER_ID,
                "body": (
                    "We have refunded US$32.50 for the cracked lamp shade "
                    "to your original payment method. That is the whole "
                    "resolution; nothing further will ship."
                ),
            },
        ),
        "Refunded 3250 cents and told the customer that is the resolution.",
    ],
}


def resolve_ticket(world: World, brief: str) -> Research:
    """The same open brief, with the ambiguity settled in one place.

    Both candidate resolutions are gathered as evidence by workers that
    cannot act on them. The lead reads both, chooses one, and writes once.
    The decision the brief left open is now recorded in a transcript instead
    of in two independent changes to the world.
    """
    findings = []
    for question in ADVISORY_QUESTIONS:
        reads = subagent.reader_registry(subagent.read_bindings(world))
        advisor = AgentLoop(
            model=FakeModel(ADVISOR_SCRIPTS),
            tools=reads,
            max_turns=6,
            budget_cents=20,
        )
        findings.append(
            subagent.compress(advisor.run(question), max_tokens=400)
        )

    lead = AgentLoop(
        model=FakeModel(RESOLVE_SCRIPT),
        tools=all_tools(world),
        policy=default_northstar_policy(),
        max_turns=12,
        budget_cents=LEAD_BUDGET_CENTS,
        principal=lead_principal(),
    )
    state = lead.start(brief, run_id="run_ch05_resolve")
    for f in findings:
        state = state.with_messages(Message(role="user", content=f.summary))
    return Research(state=lead.resume(state), findings=tuple(findings))
