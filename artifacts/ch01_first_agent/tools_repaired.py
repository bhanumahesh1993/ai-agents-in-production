"""``issue_refund`` with a derived idempotency key.

The diff against ``tools_broken`` is two lines: the key is computed from the
run and step, and it is passed on both attempts. The key is *derived*, not
generated -- a random value per attempt is a nonce, and a nonce presents a new
identity for the same intent, which defeats the entire mechanism.
"""

from __future__ import annotations

from typing import Any

from northstar_contracts import ToolSpec, World, idempotency_key

#: Same tool, one more required field, and an honest ``idempotent`` flag.
SPEC = ToolSpec(
    name="issue_refund",
    description=(
        "Refund an order exactly once. Provide the order id, the amount in "
        "integer cents, the policy reason, and the idempotency key for this "
        "step. Retrying with the same key returns the original receipt."
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
    idempotent=True,
)


def make_issue_refund(world: World, run_id: str, step_of: Any) -> Any:
    """Bind the repaired refund tool to a world.

    Args:
        world: The authoritative store.
        run_id: This run's identifier.
        step_of: Callable returning the current step number. The key must be
            stable for a logical step and different across steps, so it is
            derived from both.
    """

    def issue_refund(
        order_id: str, amount_cents: int, reason: str
    ) -> dict[str, Any]:
        """Refund once. A retry with the same key returns the receipt."""
        # Scope refunds.write, bound to this run's principal.
        key = idempotency_key(run_id, str(step_of()))
        try:
            return world.issue_refund(
                order_id, amount_cents, reason, idempotency_key=key
            )
        except Exception:
            # Same key, so the retry observes the first attempt's outcome
            # instead of creating a second one.
            return world.issue_refund(
                order_id, amount_cents, reason, idempotency_key=key
            )

    return issue_refund
