"""One builder, both deployment shapes.

``build_support_agent`` is the portability claim this chapter makes and the
artifact checks. The Kubernetes worker calls it and the hibernating edge
session calls it, and neither passes anything the other could not. What
differs between the two deployments is the *checkpointer* — a Postgres-
backed store behind a pod, or a per-session local store that survives
hibernation — and the checkpointer is an argument.

That is the whole boundary. The platform-specific code is the session shell
and the storage adapter; the decision logic is a plain module both of them
import. At the edge, where vendor concentration is the highest in the book,
that discipline is the only thing that makes the exit thinkable.
"""

from __future__ import annotations

from typing import Any

from northstar_contracts import ToolCall, World, idempotency_key
from northstar_policy import Principal, default_northstar_policy
from northstar_runtime import (
    AgentLoop,
    Checkpointer,
    FakeModel,
    MemoryCheckpointer,
    ToolRegistry,
)

__all__ = [
    "APPROVAL_THRESHOLD_CENTS",
    "ORDER",
    "PRINCIPAL",
    "REFUND_CENTS",
    "build_support_agent",
    "support_script",
]

ORDER = "NR-2026-0041827"       # US$84.00, delivered, two items
REFUND_CENTS = 3250             # the lamp shade, in integer cents
APPROVAL_THRESHOLD_CENTS = 5000

PRINCIPAL = Principal(
    user_id="CUST-8841",
    agent_id="northstar-support-agent",
    operator_id="northstar-platform",
    scopes=frozenset({"orders:read", "refunds:write"}),
)


def support_script(run_id: str) -> list[Any]:
    """The trajectory. Identical in the pod and in the edge object.

    The refund carries a key derived from the run and a stable step name,
    so a resume — whether after a pod eviction or after a hibernation —
    presents the same intent rather than a new one.
    """
    return [
        ToolCall("c1", "get_order", {"order_id": ORDER}),
        ToolCall("c2", "get_policy", {"reason": "damaged"}),
        ToolCall(
            "c3",
            "issue_refund",
            {
                "order_id": ORDER,
                "amount_cents": REFUND_CENTS,
                "reason": "damaged",
                "idempotency_key": idempotency_key(run_id, "refund"),
            },
        ),
        "Refunded 3250 cents for the cracked lamp shade.",
    ]


def build_support_agent(
    world: World | None = None,
    checkpointer: Checkpointer | None = None,
    *,
    run_id: str = "run-support",
    max_turns: int = 8,
    budget_cents: int = 120,
    threshold_cents: int = APPROVAL_THRESHOLD_CENTS,
) -> AgentLoop:
    """The Northstar support agent. The same builder everywhere.

    Args:
        world: The authoritative store. One per session at the edge; one
            shared service behind a pod. Either way the agent does not
            know the difference.
        checkpointer: Where run state is persisted. This is the *only*
            argument that differs between the two deployment shapes in
            this chapter, and it is the reason the claim holds.
        run_id: Used to derive the refund's idempotency key, so a resume
            presents the same intent rather than a new one.
        max_turns: Hard turn ceiling.
        budget_cents: Hard money ceiling. Both come from the ``Agent``
            resource's ``spec.budget`` when a controller builds this.
        threshold_cents: Above this a human decides. From
            ``spec.tools[].requiresApproval``.

    Returns:
        A configured :class:`~northstar_runtime.AgentLoop`.
    """
    store = world if world is not None else World()
    return AgentLoop(
        model=FakeModel(default=support_script(run_id)),
        tools=ToolRegistry(inject_idempotency_key=True).register_all(
            store.tools()
        ),
        checkpointer=(
            checkpointer if checkpointer is not None else MemoryCheckpointer()
        ),
        policy=default_northstar_policy(threshold_cents),
        principal=PRINCIPAL,
        max_turns=max_turns,
        budget_cents=budget_cents,
    )
