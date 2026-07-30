"""The policy decision point, as data.

Two rules decide everything this artifact demonstrates, and neither of
them can be reached by anything the model reads. They are evaluated
against facts the runtime holds independently: the scopes actually granted
in the token, and the integer amount in the call.

The default is ``DENY``. That is the correct default for a decision point
in front of money, and it is why :func:`gateway_policy` has to say
explicitly which tools are reads — a deny-by-default engine with no allow
rule blocks ``get_order`` too.
"""

from __future__ import annotations

from typing import Any

from northstar_policy import (
    Decision,
    Principal,
    Rule,
    RulesPolicyEngine,
    amount_at_or_above,
    require_scope,
)
from northstar_contracts import ToolCall

__all__ = [
    "APPROVAL_THRESHOLD_CENTS",
    "READ_TOOLS",
    "allow_reads",
    "gateway_policy",
    "policy",
]

#: Refunds at or above this need a human. Chapter 20 builds that gate;
#: here it is the third possible answer the decision point can give.
APPROVAL_THRESHOLD_CENTS = 5000

#: The tools whose worst case is a wasted read.
READ_TOOLS: frozenset[str] = frozenset(
    {"get_order", "get_policy", "search_orders"}
)

policy = RulesPolicyEngine(
    rules=[
        require_scope("issue_refund", "refunds.write"),
        amount_at_or_above("issue_refund", 5000),  # human approves
    ],
    default=Decision.DENY,
    default_reason="no rule matched; writes deny by default",
)


def allow_reads(tools: frozenset[str] = READ_TOOLS) -> Rule:
    """Allow the named read tools, and only those.

    Separating read from write is the first cut of least agency, and it is
    what lets the read-only half of a fleet run at a risk tier that needs
    far less machinery.
    """

    def when(p: Principal, c: ToolCall, ctx: dict[str, Any]) -> bool:
        return c.name in tools

    return Rule(
        name="reads.allowed",
        when=when,
        decision=Decision.ALLOW,
        reason="read-only tool, in scope",
    )


def gateway_policy() -> RulesPolicyEngine:
    """The bundle the enforcement point evaluates.

    The two refund rules are :data:`policy`'s own, reused rather than
    restated, so there is exactly one place in this artifact where the
    refund threshold and the required scope are written down.
    """
    return RulesPolicyEngine(
        rules=[allow_reads(), *policy.rules],
        default=Decision.DENY,
        default_reason="no rule matched; writes deny by default",
    )
