"""A fixed set of tickets, one of which escalates, run two ways.

The escalation is the whole point. Northstar's April incident was not a missing
span; it was a missing *edge*. The fraud review agent ran in a separate service
that began a fresh trace, so an escalated run was two unrelated traces and the
expensive half belonged to nobody. :func:`run_suite` takes ``propagate`` so the
same four tickets can be run with the edge and without it, and everything the
demo prints is a difference between those two runs.

Success is graded against each ticket's world, never against the run's own
status field. The escalated ticket ends with a `succeeded` support run, a
`succeeded` specialist run, an open fraud-review case, and no refund -- which
is a run that reported success and resolved nothing. Cost per run cannot see
that. Cost per verified success is exactly the number that can.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from cost import CostLedger
from instrument import (
    AGENT_VERSION,
    BUDGET_CENTS,
    SPECIALIST_VERSION,
    NorthstarInstrumentation,
    SideEffectIndex,
    TracedTools,
    build_context,
    instrument,
)
from northstar_contracts import Money, ToolCall, World
from northstar_policy import Principal
from northstar_runtime import AgentLoop, FakeModel, ToolRegistry
from spans import SPAN_NAMES, RunContext

__all__ = [
    "HUMAN_MINUTE_CENTS",
    "SPECIALIST_MINUTES",
    "TICKETS",
    "RunReport",
    "SuiteResult",
    "Ticket",
    "run_suite",
    "run_ticket",
]

#: Approval and escalation minutes are a real cost. An agent that is cheap in
#: tokens because it escalates constantly has moved cost, not removed it.
SPECIALIST_MINUTES = 12.0
HUMAN_MINUTE_CENTS = 75

SUPPORT_PRINCIPAL = Principal.of(
    "CUST-8841",
    "orders:read",
    "refunds:write",
    agent_id="northstar-support-agent",
)
SPECIALIST_PRINCIPAL = Principal.of(
    "CUST-9032",
    "orders:read",
    agent_id="northstar-fraud-review-agent",
)


@dataclass(frozen=True)
class Ticket:
    """One ticket, its scripted trajectory, and how to grade it.

    Attributes:
        expect_refund_cents: What must be in the refund ledger for this
            ticket to count as resolved.
        expect_open_cases: How many fraud-review cases may still be open. A
            ticket that ends with an open case is a ticket a human still owes
            an answer to, whatever the run's status says.
    """

    ticket_id: str
    goal: str
    order_id: str
    script: Sequence[Any]
    expect_refund_cents: Money
    expect_open_cases: int = 0
    escalates: bool = False
    specialist_goal: str = ""
    specialist_script: Sequence[Any] = ()


LAMP_SHADE = Ticket(
    ticket_id="TCK-4471",
    goal="Refund the cracked lamp shade on order NR-2026-0041827.",
    order_id="NR-2026-0041827",
    script=[
        ToolCall("c1", "get_order", {"order_id": "NR-2026-0041827"}),
        ToolCall(
            "c2",
            "get_policy",
            {"reason": "damaged", "sku": "NR-LAMPSHADE-03"},
        ),
        ToolCall(
            "c3",
            "issue_refund",
            {
                "order_id": "NR-2026-0041827",
                "amount_cents": 3250,
                "reason": "damaged",
            },
        ),
        ToolCall(
            "c4",
            "send_message",
            {
                "order_id": "NR-2026-0041827",
                "body": "Refunded 3250 cents for the cracked lamp shade.",
            },
        ),
        "Refunded the lamp shade. It will appear in three to five days.",
    ],
    expect_refund_cents=3250,
)

DAMAGED_MUG = Ticket(
    ticket_id="TCK-4472",
    goal="The travel mug on NR-2026-0041903 arrived cracked.",
    order_id="NR-2026-0041903",
    script=[
        ToolCall("c1", "get_order", {"order_id": "NR-2026-0041903"}),
        ToolCall(
            "c2", "get_policy", {"reason": "damaged", "sku": "NR-MUG-02"}
        ),
        ToolCall(
            "c3",
            "issue_refund",
            {
                "order_id": "NR-2026-0041903",
                "amount_cents": 3250,
                "reason": "damaged",
            },
        ),
        "Refunded the mug in full.",
    ],
    expect_refund_cents=3250,
)

WHERE_IS_IT = Ticket(
    ticket_id="TCK-4473",
    goal="Where is order NR-2026-0041827?",
    order_id="NR-2026-0041827",
    script=[
        ToolCall("c1", "get_order", {"order_id": "NR-2026-0041827"}),
        "Order NR-2026-0041827 was delivered on 11 July.",
    ],
    expect_refund_cents=0,
)

FRAUD_REVIEW = Ticket(
    ticket_id="TCK-4474",
    goal="Customer disputes the whole of order NR-2026-0042110.",
    order_id="NR-2026-0042110",
    script=[
        ToolCall("c1", "get_order", {"order_id": "NR-2026-0042110"}),
        ToolCall(
            "c2",
            "escalate_to_specialist",
            {
                "order_id": "NR-2026-0042110",
                "reason": "fraud_review",
                "notes": "24,000 cents disputed on a flagged order.",
            },
        ),
        "A specialist is reviewing this and will be in touch.",
    ],
    # Resolving this ticket means a refund decision. There is none: the case
    # is still open and nothing was refunded, which is the third of escalated
    # runs that were abandoned before a human reached them.
    expect_refund_cents=24_000,
    expect_open_cases=0,
    escalates=True,
    specialist_goal="Review flagged order NR-2026-0042110 for fraud.",
    specialist_script=[
        ToolCall("c1", "get_order", {"order_id": "NR-2026-0042110"}),
        ToolCall("c2", "get_policy", {"reason": "fraud_suspected"}),
        ToolCall(
            "c3",
            "send_message",
            {
                "order_id": "NR-2026-0042110",
                "body": "We are reviewing this order and will follow up.",
            },
        ),
        "Held for manual review; no refund issued.",
    ],
)

#: The suite, in the order the demo runs it.
TICKETS: tuple[Ticket, ...] = (
    LAMP_SHADE,
    DAMAGED_MUG,
    WHERE_IS_IT,
    FRAUD_REVIEW,
)


@dataclass
class RunReport:
    """One ticket's outcome, graded against its own world."""

    ticket: Ticket
    run_id: str
    status: str
    verified: bool
    escalated: bool
    world: World
    telemetry: list[NorthstarInstrumentation] = field(default_factory=list)
    human_minutes: float = 0.0

    @property
    def spans(self) -> list[Any]:
        """Every span the run tree emitted."""
        return [s for t in self.telemetry for s in t.spans]

    @property
    def trace_ids(self) -> set[str]:
        """Distinct trace ids across the tree. One is the healthy answer."""
        return {
            str(s.attributes.get("northstar.trace.id", "")) for s in self.spans
        }

    @property
    def missing_attributes(self) -> list[dict[str, Any]]:
        """Spans in this tree that left out a required attribute."""
        return [entry for t in self.telemetry for entry in t.missing]

    @property
    def writes_without_receipt(self) -> list[str]:
        """Write spans with no receipt id: no join key to the ledger."""
        return [
            entry for t in self.telemetry for entry in t.writes_without_receipt
        ]

    @property
    def complete(self) -> bool:
        """Whether this run's evidence would answer an incident's questions.

        Three conditions, and the second is the one Northstar failed: the
        tree has to be *one* trace. A run whose subagent starts a fresh trace
        is not partially instrumented; for attribution purposes it is
        uninstrumented.
        """
        has_shape = bool(
            [s for s in self.spans if s.name == SPAN_NAMES["run"]]
            and [s for s in self.spans if s.name == SPAN_NAMES["model"]]
        )
        return (
            has_shape
            and len(self.trace_ids) == 1
            and not self.missing_attributes
            and not self.writes_without_receipt
        )


@dataclass
class SuiteResult:
    """The whole suite, one configuration of it."""

    propagated: bool
    cost: CostLedger
    runs: list[RunReport]

    @property
    def roots(self) -> set[str]:
        """The root run ids every cost event should roll up to."""
        return {report.run_id for report in self.runs}

    @property
    def successes(self) -> list[str]:
        """Root runs that reached a correct authoritative state."""
        return [r.run_id for r in self.runs if r.verified]

    def total_cents(self) -> Money:
        """Every cent spent, attributable or not, rounded up once."""
        return self.cost.total_cents()

    def total_exact_cents(self) -> float:
        """The same total, unrounded, so the tables have resolution."""
        return self.cost.per_run_exact_cents(None)

    def unattributed_cents(self) -> Money:
        """Spend that rolls up to no root run. April's finding, as a number."""
        return self.cost.unattributed_cents(self.roots)

    def unattributed_share(self) -> float:
        """The fraction of spend nobody can attribute to a run."""
        return self.cost.unattributed_share(self.roots)

    def cost_per_run(self) -> float:
        """The wrong denominator, reported so the right one has a contrast."""
        return self.total_exact_cents() / max(1, len(self.runs))

    def human_minutes(self) -> float:
        """Specialist and approver minutes this suite consumed."""
        return sum(r.human_minutes for r in self.runs)

    def cost_per_success(self) -> float:
        """Total spend, human time included, per verified success."""
        return self.cost.cost_per_success(
            attempted=[r.run_id for r in self.runs],
            succeeded=self.successes,
            human_minutes=self.human_minutes(),
            minute_cents=HUMAN_MINUTE_CENTS,
        )

    def completeness(self) -> float:
        """Fraction of runs whose evidence is whole. An SLI, not a hope."""
        if not self.runs:
            return 1.0
        return sum(1 for r in self.runs if r.complete) / len(self.runs)

    def table(self) -> list[dict[str, Any]]:
        """One row per run, in the shape the demo prints."""
        return [
            {
                "ticket": r.ticket.ticket_id,
                "run_id": r.run_id,
                "status": r.status,
                "verified": r.verified,
                "escalated": r.escalated,
                "cents": round(self.cost.per_run_exact_cents(r.run_id), 4),
                "traces": len(r.trace_ids),
                "complete": r.complete,
            }
            for r in self.runs
        ]


def run_ticket(
    ticket: Ticket,
    cost: CostLedger,
    *,
    propagate: bool = True,
    exporter: str = "memory",
    stream: Any | None = None,
) -> RunReport:
    """Run one ticket, and its specialist child if it escalates."""
    world = World()
    base = ToolRegistry(inject_idempotency_key=True).register_all(
        world.tools()
    )
    run_id = f"run-{ticket.ticket_id.lower()}"
    ctx = build_context(
        run_id,
        ticket.goal,
        principal=SUPPORT_PRINCIPAL,
        specs=world.tool_specs(),
        agent_version=AGENT_VERSION,
    )
    cost.link(run_id, run_id)

    effects = SideEffectIndex()
    loop = AgentLoop(
        model=FakeModel(scripts={ticket.goal: list(ticket.script)}),
        tools=TracedTools(base, run_id, effects),
        budget_cents=BUDGET_CENTS,
        max_turns=8,
        principal=SUPPORT_PRINCIPAL,
    )
    telemetry = instrument(
        loop, ctx, exporter, cost=cost, effects=effects, stream=stream
    )
    state = loop.run(ticket.goal, run_id=run_id)

    report = RunReport(
        ticket=ticket,
        run_id=run_id,
        status=state.status,
        verified=False,
        escalated=ticket.escalates,
        world=world,
        telemetry=[telemetry],
    )

    if ticket.escalates:
        report.human_minutes = SPECIALIST_MINUTES
        child_telemetry = _run_specialist(
            ticket,
            ctx,
            telemetry,
            world,
            base,
            cost,
            propagate=propagate,
            exporter=exporter,
            stream=stream,
        )
        report.telemetry.append(child_telemetry)

    report.verified = _verified(ticket, world)
    return report


def _run_specialist(
    ticket: Ticket,
    parent: RunContext,
    parent_telemetry: NorthstarInstrumentation,
    world: World,
    base: ToolRegistry,
    cost: CostLedger,
    *,
    propagate: bool,
    exporter: str,
    stream: Any | None,
) -> NorthstarInstrumentation:
    """Run the fraud review agent, with the trace edge or without it."""
    child_run_id = f"{parent.run_id}-fraud"
    child_ctx = (
        parent.child(child_run_id, SPECIALIST_VERSION)
        if propagate
        else parent.orphan(child_run_id, SPECIALIST_VERSION)
    )
    child_ctx.principal = SPECIALIST_PRINCIPAL
    child_ctx.goal = ticket.specialist_goal

    parent_telemetry.record_handoff(
        child_ctx,
        reason="fraud_review",
        budget_handed=parent.budget_remaining,
        at=parent_telemetry.spans[-1].end_time if parent_telemetry.spans else 0.0,
    )
    if propagate:
        # The eleven-line fix: the child's spend rolls up to the run that
        # caused it rather than to a service.
        cost.link(child_run_id, parent.run_id)

    effects = SideEffectIndex()
    loop = AgentLoop(
        model=FakeModel(
            scripts={ticket.specialist_goal: list(ticket.specialist_script)}
        ),
        tools=TracedTools(base, child_run_id, effects),
        budget_cents=BUDGET_CENTS,
        max_turns=8,
        principal=SPECIALIST_PRINCIPAL,
    )
    telemetry = instrument(
        loop, child_ctx, exporter, cost=cost, effects=effects, stream=stream
    )
    loop.run(ticket.specialist_goal, run_id=child_run_id)
    return telemetry


def _verified(ticket: Ticket, world: World) -> bool:
    """Grade one ticket against the world, not against the transcript."""
    refunded = world.total_refunded_cents(ticket.order_id)
    open_cases = sum(
        1
        for case in world.escalations
        if case["order_id"] == ticket.order_id and case["status"] == "open"
    )
    return (
        refunded == ticket.expect_refund_cents
        and open_cases == ticket.expect_open_cases
    )


def run_suite(
    *,
    propagate: bool = True,
    exporter: str = "memory",
    stream: Any | None = None,
    tickets: Sequence[Ticket] = TICKETS,
) -> SuiteResult:
    """Run the whole suite in one configuration.

    Args:
        propagate: Carry the trace context and the cost root across the
            escalation hop. ``False`` reproduces April exactly.
        exporter: Passed through to :func:`instrument.instrument`.
        stream: Output stream for the console exporter.
        tickets: Override the suite, which is how a reader adds a case.
    """
    cost = CostLedger()
    runs = [
        run_ticket(
            ticket,
            cost,
            propagate=propagate,
            exporter=exporter,
            stream=stream,
        )
        for ticket in tickets
    ]
    return SuiteResult(propagated=propagate, cost=cost, runs=runs)
