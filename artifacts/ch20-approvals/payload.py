"""The approval payload: what a person must see to decide.

Six things belong in it, and each has a rule about where it comes from.
The one that carries the incident is the **preview**: it is a dry run
against the target system, not the agent's description of the effect. A
refund already issued that morning appears as a row in the preview and
does not appear in any summary the model writes, which is the difference
between a decidable question and a card with a number on it.

Nothing here renders prose for the model to read. The payload is data;
:func:`render` turns it into the YAML the file-backed inbox stores and a
reviewer reads.
"""

from __future__ import annotations

from typing import Any

from northstar_contracts import Money, ToolCall, World
from northstar_policy import Principal

__all__ = [
    "IMPACT",
    "approval_payload",
    "evidence_from",
    "preview_refund",
    "render",
]

#: The cost of being wrong, in both directions, per tool. Approvers
#: systematically default to whichever side has an unstated cost, so both
#: sides are stated. These are Northstar's words, reviewed once, rather
#: than a sentence the model writes fresh on every request.
IMPACT: dict[str, dict[str, str]] = {
    "issue_refund": {
        "if_wrong_approve": "cents leave the business; clawback is manual.",
        "if_wrong_reject": "Customer waits for a specialist callback.",
    },
    "send_message": {
        "if_wrong_approve": "The customer has already read it. No undo.",
        "if_wrong_reject": "The customer is not told; the case stays open.",
    },
}


def preview_refund(world: World, order_id: str, amount_cents: Money) -> dict:
    """Dry-run a refund against the authoritative store.

    Reads only. Everything here comes from the world rather than from the
    transcript, which is the whole point: the agent does not get to
    describe the state it is about to change.

    Returns:
        The order's current position and the resulting balance, plus a
        ``version`` token. The token is the sixth resume check's input:
        re-read it before commit and two runs cannot each hold a valid
        approval for a refund on the same order and both execute.
    """
    order = world.orders.get(order_id)
    if order is None:
        return {"error": f"no order {order_id!r}"}
    already = world.total_refunded_cents(order_id)
    rows = world.refunds_for(order_id)
    return {
        "order_total_cents": order["total_cents"],
        "refunds_already_issued_cents": already,
        "refunds_already_issued_count": len(rows),
        "resulting_balance_cents": order["total_cents"] - already
        - int(amount_cents),
        "flags": list(order["flags"]),
        # A row version. Cheap here, an ETag in a real service, and the
        # thing that makes "the world has not moved" checkable.
        "version": f"{order_id}:{len(rows)}:{already}",
    }


def evidence_from(observations: list[dict[str, Any]]) -> list[str]:
    """The specific tool results the conclusion rests on.

    An approver who can see that the policy lookup returned
    ``policy_version=2026-07-01`` can check the reasoning. An approver who
    is told "policy permits this" can only check the agent's confidence.
    """
    lines: list[str] = []
    for observation in observations:
        tool = observation.get("tool")
        content = observation.get("content")
        if not isinstance(content, dict):
            continue
        if tool == "get_policy":
            rules = content.get("rules") or [{}]
            first = rules[0] if isinstance(rules, list) and rules else {}
            lines.append(
                f"get_policy policy_version="
                f"{content.get('policy_version')} "
                f"reason={first.get('reason')} "
                f"eligible={first.get('eligible')}"
            )
        elif tool == "get_order":
            lines.append(
                f"get_order status={content.get('status')} "
                f"total_cents={content.get('total_cents')} "
                f"refunded_cents={content.get('refunded_cents')}"
            )
    return lines


def approval_payload(
    call: ToolCall,
    *,
    fingerprint: str,
    principal: Principal,
    tool_version: str,
    world: World,
    observations: list[dict[str, Any]] | None = None,
    reason: str = "",
    scope: str = "refunds.write",
    expires_at: float = 0.0,
    on_expiry: str = "reject",
) -> dict[str, Any]:
    """Assemble the six-part payload for one call.

    Args:
        call: The exact call, arguments verbatim, no rounding, no summary.
        fingerprint: What the eventual decision binds to.
        principal: Who is asking, and on whose behalf.
        tool_version: The contract version the approval is issued against.
        world: The target system, dry-run for the preview.
        observations: Tool results the run has already seen, for evidence.
        reason: The agent's claim. Under evaluation, not evidence.
        scope: The credential the call will use.
        expires_at: Absolute expiry. Expiry defaults to rejection; a
            pending approval that becomes an approval by timing out is a
            system that approves everything slowly.
        on_expiry: What happens at ``expires_at``.
    """
    order_id = str(call.arguments.get("order_id", ""))
    amount = int(call.arguments.get("amount_cents", 0) or 0)
    return {
        "fingerprint": fingerprint,
        "call": {
            "tool": call.name,
            "tool_version": tool_version,
            "arguments": dict(call.arguments),
        },
        "preview": preview_refund(world, order_id, amount),
        "reason": reason,
        "evidence": evidence_from(observations or []),
        "impact": dict(IMPACT.get(call.name, {})),
        "envelope": {
            "principal": (
                f"agent:{principal.agent_id} on behalf of "
                f"user:{principal.user_id}"
            ),
            "operator": principal.operator_id,
            "scope": scope,
            "expires_at": expires_at,
            "on_expiry": on_expiry,
        },
    }


def render(payload: dict[str, Any], indent: int = 0) -> str:
    """Render a payload as the YAML an inbox stores and a reviewer reads.

    Deliberately a printer rather than a serializer: it handles the shapes
    :func:`approval_payload` produces and nothing else. There is no YAML
    dependency in this repository, and adding one to pretty-print six
    fields would be a poor trade.
    """
    pad = " " * indent
    lines: list[str] = []
    for key, value in payload.items():
        if isinstance(value, dict):
            lines.append(f"{pad}{key}:")
            lines.append(render(value, indent + 2))
        elif isinstance(value, list):
            lines.append(f"{pad}{key}:")
            for item in value:
                lines.append(f"{pad}  - {item}")
            if not value:
                lines[-1] = f"{pad}{key}: []"
        else:
            lines.append(f"{pad}{key}: {value}")
    return "\n".join(line for line in lines if line)
