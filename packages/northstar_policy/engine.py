"""The policy decision point: allow, deny, or ask a human.

The rule this package exists to enforce is that *the agent does not decide
what it is allowed to do*. The model proposes a tool call; something
outside the model's context decides whether that call may proceed. If the
authorisation logic lives in the prompt, it is not authorisation — it is a
suggestion, and prompt injection is the counter-argument.

So the policy engine takes three inputs the model cannot forge:

* the :class:`Principal` — who the user is, which agent is acting for them,
  and which scopes were actually granted;
* the :class:`~northstar_contracts.models.ToolCall` as it will really be
  dispatched, arguments included;
* a context dict the runtime fills in (spend so far, step, run id).

and returns one of three answers.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Protocol, runtime_checkable

from northstar_contracts import Money, ToolCall, ToolSpec

__all__ = [
    "Decision",
    "PolicyEngine",
    "PolicyVerdict",
    "Principal",
    "Rule",
    "RulesPolicyEngine",
    "amount_at_or_above",
    "default_northstar_policy",
    "deny_tool",
    "flagged_order",
    "require_scope",
]


class Decision(Enum):
    """What the policy decision point says about one proposed call."""

    ALLOW = "allow"
    DENY = "deny"
    REQUIRE_APPROVAL = "require_approval"


@dataclass(frozen=True)
class Principal:
    """Who is acting, on whose behalf, under whose operational control.

    Three identities, not one. Collapsing them is the most common identity
    mistake in agent systems, and it is what turns a prompt injection into
    a breach: if the agent holds the user's full session, anything that
    talks its way past the model inherits everything the user can do.

    Args:
        user_id: The human the work is being done for. May be ``None`` for
            a background agent with no requesting user.
        agent_id: The agent doing the work. This is a workload identity,
            not a person.
        operator_id: The team accountable for running the agent.
        scopes: The permissions actually granted for *this* run. Narrow and
            short-lived beats broad and permanent.
    """

    user_id: str | None = None
    agent_id: str = "northstar-support-agent"
    operator_id: str = "northstar-platform"
    scopes: frozenset[str] = field(default_factory=frozenset)

    def has(self, scope: str) -> bool:
        """Whether this principal holds ``scope``."""
        return scope in self.scopes

    @classmethod
    def of(
        cls,
        user_id: str | None = None,
        *scopes: str,
        agent_id: str = "northstar-support-agent",
        operator_id: str = "northstar-platform",
    ) -> Principal:
        """Convenience constructor: ``Principal.of("CUST-8841", "orders:read")``."""
        return cls(
            user_id=user_id,
            agent_id=agent_id,
            operator_id=operator_id,
            scopes=frozenset(scopes),
        )

    def to_dict(self) -> dict[str, Any]:
        """JSON-serialisable form, for journals and span attributes."""
        return {
            "user_id": self.user_id,
            "agent_id": self.agent_id,
            "operator_id": self.operator_id,
            "scopes": sorted(self.scopes),
        }


@runtime_checkable
class PolicyEngine(Protocol):
    """Anything that can decide on a tool call.

    Deliberately tiny. A real deployment usually delegates to an external
    decision point (OPA, Cedar, a cloud authorisation service); this
    interface is what the agent runtime needs and all it should know.
    """

    def evaluate(
        self,
        principal: Principal,
        call: ToolCall,
        ctx: dict[str, Any],
    ) -> Decision:
        """Return the decision for one proposed call."""
        ...


@dataclass(frozen=True)
class PolicyVerdict:
    """A decision plus the reason for it.

    :meth:`RulesPolicyEngine.evaluate` returns the bare
    :class:`Decision` because that is the contract the runtime codes
    against. Humans reviewing an incident need the rule name too, so
    :meth:`RulesPolicyEngine.evaluate_verbose` returns this instead.
    """

    decision: Decision
    rule: str
    reason: str

    def to_dict(self) -> dict[str, Any]:
        """JSON-serialisable form."""
        return {
            "decision": self.decision.value,
            "rule": self.rule,
            "reason": self.reason,
        }


#: A rule predicate. Pure, cheap, and side-effect free: it may be evaluated
#: more than once for the same call during a replay.
Predicate = Callable[[Principal, ToolCall, dict[str, Any]], bool]


@dataclass(frozen=True)
class Rule:
    """One named rule. First match wins, so order matters.

    Args:
        name: Stable identifier. It goes in the audit record; renaming a
            rule breaks the trail, so treat the name as an interface.
        when: Predicate deciding whether the rule applies.
        decision: What to return when it applies.
        reason: Human-readable justification, shown to the approver.
    """

    name: str
    when: Predicate
    decision: Decision
    reason: str = ""


class RulesPolicyEngine:
    """A first-match-wins rule list with an explicit default.

    Args:
        rules: Evaluated in order. The first whose predicate returns
            ``True`` decides.
        default: Returned when no rule matches. ``DENY`` is the right
            default for anything that writes; this engine defaults to
            ``ALLOW`` and expects you to deny writes explicitly, because a
            deny-by-default engine with no rules blocks the read tools too
            and readers give up on the example. In production, invert it.
    """

    def __init__(
        self,
        rules: Sequence[Rule] = (),
        default: Decision = Decision.ALLOW,
        default_reason: str = "no rule matched",
    ) -> None:
        self.rules: list[Rule] = list(rules)
        self.default = default
        self.default_reason = default_reason

    def add(self, rule: Rule) -> RulesPolicyEngine:
        """Append a rule and return ``self``, for chaining."""
        self.rules.append(rule)
        return self

    def evaluate(
        self,
        principal: Principal,
        call: ToolCall,
        ctx: dict[str, Any],
    ) -> Decision:
        """Return the decision for one proposed call."""
        return self.evaluate_verbose(principal, call, ctx).decision

    def evaluate_verbose(
        self,
        principal: Principal,
        call: ToolCall,
        ctx: dict[str, Any],
    ) -> PolicyVerdict:
        """Return the decision together with the rule that produced it."""
        for rule in self.rules:
            if rule.when(principal, call, ctx):
                return PolicyVerdict(rule.decision, rule.name, rule.reason)
        return PolicyVerdict(self.default, "default", self.default_reason)


# --------------------------------------------------------------- rule helpers


def require_scope(tool: str, scope: str) -> Rule:
    """Deny ``tool`` unless the principal holds ``scope``."""
    return Rule(
        name=f"{tool}.requires.{scope}",
        when=lambda p, c, ctx: c.name == tool and not p.has(scope),
        decision=Decision.DENY,
        reason=f"{tool} requires the {scope} scope",
    )


def amount_at_or_above(
    tool: str,
    threshold_cents: Money,
    field_name: str = "amount_cents",
    decision: Decision = Decision.REQUIRE_APPROVAL,
) -> Rule:
    """Gate ``tool`` when a money field reaches ``threshold_cents``.

    The comparison is ``>=`` on purpose. A threshold you can sit exactly on
    is a threshold someone will sit exactly on.
    """

    def when(p: Principal, c: ToolCall, ctx: dict[str, Any]) -> bool:
        if c.name != tool:
            return False
        value = c.arguments.get(field_name)
        return isinstance(value, int) and value >= threshold_cents

    return Rule(
        name=f"{tool}.{field_name}.at_or_above.{threshold_cents}",
        when=when,
        decision=decision,
        reason=(
            f"{field_name} is at or above the {threshold_cents}c threshold"
        ),
    )


def deny_tool(tool: str, reason: str = "") -> Rule:
    """Deny a tool outright, whatever the arguments."""
    return Rule(
        name=f"{tool}.denied",
        when=lambda p, c, ctx: c.name == tool,
        decision=Decision.DENY,
        reason=reason or f"{tool} is not permitted for this principal",
    )


def flagged_order(
    tool: str,
    flag: str,
    flagged_order_ids: Sequence[str],
    decision: Decision = Decision.REQUIRE_APPROVAL,
) -> Rule:
    """Gate ``tool`` when it touches an order carrying ``flag``.

    The flagged ids are passed in rather than read from the world: a policy
    decision point that has to query the system it is protecting is a
    policy decision point that fails open when that system is down.
    """
    flagged = set(flagged_order_ids)

    def when(p: Principal, c: ToolCall, ctx: dict[str, Any]) -> bool:
        return c.name == tool and c.arguments.get("order_id") in flagged

    return Rule(
        name=f"{tool}.flagged.{flag}",
        when=when,
        decision=decision,
        reason=f"order is flagged {flag}",
    )


def write_tools_of(specs: Sequence[ToolSpec]) -> list[str]:
    """Names of every tool in ``specs`` that mutates the world."""
    return [s.name for s in specs if s.writes]


def default_northstar_policy(
    threshold_cents: Money = 5000,
    flagged_order_ids: Sequence[str] = ("NR-2026-0042110",),
) -> RulesPolicyEngine:
    """The policy the book's Northstar examples run under.

    In order:

    1. Refunds need the ``refunds:write`` scope. No scope, no refund, and
       no amount of persuasion in the transcript changes that.
    2. Refunds against a fraud-flagged order always need a human.
    3. Refunds at or above ``threshold_cents`` need a human.
    4. Everything else — the read tools, messages, escalation — is allowed.

    Args:
        threshold_cents: The autonomy budget for a single refund.
        flagged_order_ids: Orders under fraud review.
    """
    return RulesPolicyEngine(
        rules=[
            require_scope("issue_refund", "refunds:write"),
            flagged_order("issue_refund", "fraud_review", flagged_order_ids),
            amount_at_or_above("issue_refund", threshold_cents),
        ],
        default=Decision.ALLOW,
        default_reason="read-only or low-risk tool",
    )
