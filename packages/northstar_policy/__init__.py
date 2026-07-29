"""Policy decision point, approvals, and budgets.

The three mechanisms that decide what an agent is *allowed* to do, kept
outside the agent so that nothing the model reads can change them::

    from northstar_policy import (
        BudgetGuard, Decision, Principal, default_northstar_policy,
    )
"""

from __future__ import annotations

from .approvals import (
    ApprovalDecision,
    ApprovalRequest,
    ApprovalStatus,
    ApprovalStore,
    approval_fingerprint,
)
from .budget import (
    BudgetExceeded,
    BudgetGuard,
    BudgetKind,
    TurnLimitExceeded,
)
from .engine import (
    Decision,
    PolicyEngine,
    PolicyVerdict,
    Principal,
    Rule,
    RulesPolicyEngine,
    amount_at_or_above,
    default_northstar_policy,
    deny_tool,
    flagged_order,
    require_scope,
    write_tools_of,
)

__version__ = "1.0.0"

__all__ = [
    "ApprovalDecision",
    "ApprovalRequest",
    "ApprovalStatus",
    "ApprovalStore",
    "BudgetExceeded",
    "BudgetGuard",
    "BudgetKind",
    "Decision",
    "PolicyEngine",
    "PolicyVerdict",
    "Principal",
    "Rule",
    "RulesPolicyEngine",
    "TurnLimitExceeded",
    "__version__",
    "amount_at_or_above",
    "approval_fingerprint",
    "default_northstar_policy",
    "deny_tool",
    "flagged_order",
    "require_scope",
    "write_tools_of",
]
