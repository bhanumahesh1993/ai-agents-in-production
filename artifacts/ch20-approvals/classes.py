"""Action classes, and the policy that enforces them.

The question "should a human approve this?" has no good answer at the level
of a tool. ``issue_refund`` at 300 cents and ``issue_refund`` at 240,000
cents are the same function and different decisions. The unit that carries
a class is the triple of tool, argument range, and resource scope.

Four classes cover production work, and the fourth is not an approval class
at all. **Never permitted** means there is no in-band decision that grants
the action, because the capability is not delegated under any argument. A
refund to an account other than the original payer is not a hard approval
at Northstar; it is not a thing the support agent can do, and the tool does
not expose the parameter. :func:`refund_to_non_payer` demonstrates that the
refusal comes from the schema rather than from anyone's vigilance.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from northstar_contracts import ToolCall, ToolResult
from northstar_policy import (
    Decision,
    Principal,
    Rule,
    RulesPolicyEngine,
    amount_at_or_above,
    flagged_order,
    require_scope,
)
from northstar_runtime import ToolRegistry

__all__ = [
    "ACTION_CLASSES",
    "APPROVAL_THRESHOLD_CENTS",
    "FLAGGED_ORDERS",
    "POLICY_VERSION",
    "ActionClass",
    "class_for",
    "northstar_policy_bundle",
    "refund_to_non_payer",
]

ClassName = Literal[
    "automatic", "sampled", "always_approved", "never_permitted"
]
Reversibility = Literal[
    "not_applicable",
    "automatic",
    "human_action",
    "compensatable",
    "irreversible",
]

#: Refunds at or above this need a human. The chapter's prose table reads
#: "at or under 5,000: sampled" and "above 5,000: always approved"; the
#: rule is ``>=`` and the rule is what runs, because a threshold you can
#: sit exactly on is a threshold someone will sit exactly on.
APPROVAL_THRESHOLD_CENTS = 5000

#: Orders under fraud review. Passed in rather than queried, so the
#: decision point does not fail open when the order service is down.
FLAGGED_ORDERS: tuple[str, ...] = ("NR-2026-0042110",)

#: Bumping this invalidates parked approvals, which is the fourth resume
#: check. It is a version rather than a hash so a reviewer can read it.
POLICY_VERSION = "2026-07-01"


@dataclass(frozen=True)
class ActionClass:
    """One row of Northstar's assignment table.

    Args:
        name: Which of the four classes.
        reversibility: How the effect is undone, if it can be.
        sample_rate: Fraction reviewed afterwards, for the sampled class.
            Sampling is a measurement, not a safety control: it is how you
            discover a class assignment was wrong before an incident does.
        detected_by: What would catch a wrong call, and roughly how fast.
            Detectability is the axis nobody writes down and it dominates.
    """

    name: ClassName
    reversibility: Reversibility
    sample_rate: float = 0.0
    detected_by: str = ""


#: Northstar's assignment, after the June review.
ACTION_CLASSES: dict[str, ActionClass] = {
    "get_order": ActionClass("automatic", "not_applicable"),
    "get_policy": ActionClass("automatic", "not_applicable"),
    "search_orders": ActionClass("automatic", "not_applicable"),
    "escalate_to_specialist": ActionClass(
        "automatic", "human_action", detected_by="fraud queue, minutes"
    ),
    "issue_refund": ActionClass(
        "sampled",
        "compensatable",
        sample_rate=0.05,
        detected_by="nightly reconciliation, under 24h",
    ),
    "send_message": ActionClass(
        "sampled",
        "irreversible",
        sample_rate=0.02,
        detected_by="customer reply, unbounded",
    ),
}


def class_for(call: ToolCall) -> ActionClass:
    """The class of one call, argument range included.

    A refund above the threshold is a different class from the same tool
    below it, which is the whole reason the class is not a property of the
    tool.
    """
    base = ACTION_CLASSES.get(
        call.name, ActionClass("never_permitted", "irreversible")
    )
    if call.name == "issue_refund":
        amount = call.arguments.get("amount_cents")
        if isinstance(amount, int) and amount >= APPROVAL_THRESHOLD_CENTS:
            return ActionClass(
                "always_approved",
                "compensatable",
                detected_by=base.detected_by,
            )
        if call.arguments.get("order_id") in FLAGGED_ORDERS:
            return ActionClass(
                "always_approved",
                "compensatable",
                detected_by=base.detected_by,
            )
    return base


def allow_within_class() -> Rule:
    """Allow anything whose class is automatic or sampled.

    Last in the list, so every refusal above it fires first. A sampled
    action executes and is reviewed afterwards; the sampling happens in the
    review pipeline, not in the enforcement path, because a control that
    only fires 5% of the time is not a control.
    """

    def when(p: Principal, c: ToolCall, ctx: dict[str, Any]) -> bool:
        return class_for(c).name in ("automatic", "sampled")

    return Rule(
        name="action_class.permits",
        when=when,
        decision=Decision.ALLOW,
        reason="action class is automatic or sampled",
    )


def northstar_policy_bundle(
    threshold_cents: int = APPROVAL_THRESHOLD_CENTS,
    flagged: tuple[str, ...] = FLAGGED_ORDERS,
) -> RulesPolicyEngine:
    """The decision point Northstar runs after the June review.

    In order: refunds need the scope, a flagged order always needs a human,
    a refund at or above the threshold always needs a human, anything whose
    class is automatic or sampled proceeds, and everything else is denied.
    """
    return RulesPolicyEngine(
        rules=[
            require_scope("issue_refund", "refunds.write"),
            flagged_order("issue_refund", "fraud_review", flagged),
            amount_at_or_above("issue_refund", threshold_cents),
            allow_within_class(),
        ],
        default=Decision.DENY,
        default_reason="no rule matched; writes deny by default",
    )


def refund_to_non_payer(
    registry: ToolRegistry,
    order_id: str,
    amount_cents: int,
    destination_account: str,
) -> ToolResult:
    """Try the never-permitted action, and watch the schema refuse it.

    There is no policy rule for this and there should not be one. The tool
    contract does not expose a destination account, so the argument is
    rejected at validation, before any principal, scope, or approver is
    consulted. If your answer to a dangerous capability is "we will require
    approval for it", you have made a person the last line of defence
    against something a schema could have prevented.

    Returns:
        The failed :class:`~northstar_contracts.models.ToolResult`.
    """
    return registry.dispatch(
        ToolCall(
            "never-permitted",
            "issue_refund",
            {
                "order_id": order_id,
                "amount_cents": amount_cents,
                "reason": "damaged",
                "destination_account": destination_account,
            },
        ),
        run_id="run_never_permitted",
        step=0,
    )
