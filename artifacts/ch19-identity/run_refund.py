"""One Northstar refund, end to end, under delegated authority.

The same trajectory runs twice in the demo: once with ``refunds.write`` in
the user's grant, and once with it withheld. Nothing else changes. The
difference is not a different prompt, a different model, or a different
tool. It is one entry in a mapping the agent cannot see.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from authz_server import AuthorizationServer
from broker import TokenBroker
from gateway import DecisionLog, TokenBoundTools, ToolGateway
from northstar_contracts import (
    EventLog,
    Message,
    RunState,
    ToolCall,
    ToolResult,
    World,
    idempotency_key,
)
from northstar_policy import Principal
from northstar_runtime import AgentLoop, FakeModel, ToolRegistry
from policy import gateway_policy

__all__ = [
    "AMOUNT",
    "ORDER",
    "RUN_ID",
    "SKU",
    "USER",
    "RefundRun",
    "caller",
    "refund_call",
    "run_refund",
    "script",
]

ORDER = "NR-2026-0041827"       # US$84.00, delivered, two items
SKU = "NR-LAMPSHADE-03"
AMOUNT = 3250                   # cents. Always integer cents.
USER = "CUST-8841"              # the customer who owns that order
RUN_ID = "run_ch19_refund"
GOAL = "Customer says the lamp shade in this order arrived cracked."

# artifacts/ch19-identity/run_refund.py (excerpt)
caller = Principal(
    user_id="CUST-8841",                  # on whose behalf
    agent_id="northstar-support-agent",   # which workload
    operator_id="northstar-platform",     # who is accountable
    scopes=frozenset({"orders.read"}),    # granted, not requested
)


def refund_call(call_id: str = "c3") -> ToolCall:
    """The refund the agent proposes, with its derived idempotency key.

    Identity decides whether the write is permitted. Idempotency decides
    what a second attempt at a permitted write does. They are independent
    mechanisms and you need both, which is why the key is here even though
    this chapter is about the token.
    """
    return ToolCall(
        call_id,
        "issue_refund",
        {
            "order_id": ORDER,
            "amount_cents": AMOUNT,
            "reason": "damaged",
            "idempotency_key": idempotency_key(RUN_ID, "refund"),
        },
    )


def script() -> list[Any]:
    """Read the order, check the policy, refund, reply.

    Scripted so the run is identical every time. The interesting variable
    in this artifact is the grant, not the model.
    """
    return [
        ToolCall("c1", "get_order", {"order_id": ORDER}),
        ToolCall("c2", "get_policy", {"sku": SKU, "reason": "damaged"}),
        refund_call(),
        "Refunded 3250 cents for the damaged lamp shade. Sorry about that.",
    ]


class GatewayTools(ToolRegistry):
    """Adapts the gateway to the runtime's registry interface.

    The loop knows how to dispatch through a ``ToolRegistry``; the gateway
    takes a principal and a context as well. This class is the join, and it
    is the only place the two shapes meet. It deliberately does *not* stamp
    idempotency keys: in this artifact the key is part of the proposed call,
    so it is part of what the policy evaluated.
    """

    def __init__(
        self,
        gateway: ToolGateway,
        principal: Principal,
        bindings: list[tuple[Any, Any]],
    ) -> None:
        super().__init__(inject_idempotency_key=False)
        self.register_all(bindings)
        self.gateway = gateway
        self.principal = principal

    def dispatch(
        self,
        call: ToolCall,
        run_id: str | None = None,
        step: int | None = None,
    ) -> ToolResult:
        """Send the call through the enforcement point. No bypass path."""
        return self.gateway.dispatch(
            self.principal,
            call,
            {"run_id": run_id or RUN_ID, "step": step or 0},
        )


@dataclass
class RefundRun:
    """Everything the demo and the tests need to look at afterwards."""

    world: World
    state: RunState
    decisions: EventLog
    server: AuthorizationServer
    broker: TokenBroker
    gateway: ToolGateway
    tools: TokenBoundTools

    @property
    def refunded_cents(self) -> int:
        """What actually landed in the world, not what the agent said."""
        return self.world.total_refunded_cents(ORDER)

    @property
    def decision_records(self) -> list[dict[str, Any]]:
        """One record per policy decision, in order."""
        return self.decisions.of_type("tool.called")

    def decision_for(self, tool: str) -> dict[str, Any] | None:
        """The decision record for one tool, if there is one."""
        for record in self.decision_records:
            if record["payload"]["tool"] == tool:
                return record
        return None

    @property
    def final_text(self) -> str | None:
        """What a customer would read."""
        return self.state.final_text


def run_refund(*, grant: frozenset[str] | None = None) -> RefundRun:
    """Run the refund under a grant, and report what happened.

    Args:
        grant: Scopes the *user* actually holds at the authorization
            server. Defaults to the full grant a support interaction needs.
            Pass ``frozenset({"orders.read"})`` to withhold the refund
            scope and watch the call fail closed.

    Returns:
        A :class:`RefundRun` holding the world, the run state, and the
        decision log.
    """
    if grant is None:
        grant = frozenset({"orders.read", "refunds.write"})

    world = World()
    server = AuthorizationServer(grants={f"user:{USER}": grant})
    broker = TokenBroker(server)
    decisions = EventLog()
    tools = TokenBoundTools(
        ToolRegistry(inject_idempotency_key=False).register_all(
            world.tools()
        ),
        server,
    )
    gateway = ToolGateway(
        policy=DecisionLog(gateway_policy(), decisions),
        broker=broker,
        tools=tools,
    )

    # The principal's scope set mirrors the grant, because a principal that
    # claims a scope the authorization server will not mint is a principal
    # that lies to the policy engine. The server is still the authority:
    # the exchange checks the grant, not this field.
    principal = Principal(
        user_id=caller.user_id,
        agent_id=caller.agent_id,
        operator_id=caller.operator_id,
        scopes=grant,
    )

    loop = AgentLoop(
        model=FakeModel(default=script()),
        tools=GatewayTools(gateway, principal, world.tools()),
        # No loop-level policy on purpose. There is exactly one enforcement
        # point in this artifact, and it is the gateway.
        policy=None,
        principal=principal,
        max_turns=6,
    )
    state = loop.run(GOAL, run_id=RUN_ID)
    return RefundRun(
        world=world,
        state=state,
        decisions=decisions,
        server=server,
        broker=broker,
        gateway=gateway,
        tools=tools,
    )


def observations(state: RunState) -> list[dict[str, Any]]:
    """Every tool observation the model read, in order."""
    return [
        m.content
        for m in state.messages
        if isinstance(m, Message)
        and m.role == "tool"
        and isinstance(m.content, dict)
    ]
