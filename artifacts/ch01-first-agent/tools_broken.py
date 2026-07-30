"""``issue_refund`` as Northstar first wrote it.

The bug is not the retry. Retrying a flaky internal API is reasonable. The bug
is that the call carries no way for the refund service to recognise the second
request as the same intent, so "try again" and "pay again" are the same
instruction.
"""

from __future__ import annotations

from typing import Any

from northstar_contracts import ToolSpec, World

#: The contract the model sees. Note what is missing from the schema.
SPEC = ToolSpec(
    name="issue_refund",
    description=(
        "Refund an order. Provide the order id, the amount in integer "
        "cents, and the policy reason."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "order_id": {"type": "string"},
            "amount_cents": {"type": "integer"},
            "reason": {"type": "string"},
        },
        "required": ["order_id", "amount_cents", "reason"],
    },
    output_schema={"type": "object"},
    writes=True,
    idempotent=False,
)


def make_issue_refund(world: World) -> Any:
    """Bind the broken refund tool to a world."""

    def issue_refund(
        order_id: str, amount_cents: int, reason: str
    ) -> dict[str, Any]:
        """Refund an order. No idempotency key: a retry pays twice."""
        # Runs under the support-agent principal, scope refunds.write.
        try:
            return world.issue_refund(order_id, amount_cents, reason)
        except Exception:
            # The write may have committed. This code cannot tell, and it
            # is about to make that ambiguity expensive.
            return world.issue_refund(order_id, amount_cents, reason)

    return issue_refund
