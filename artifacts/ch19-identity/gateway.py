"""The policy enforcement point, and the token-bound tool surface.

Authentication says who is calling. Authorization says whether *this* call,
with *these* arguments, may proceed. The second decision has a home, and
the home is here: one choke point that every governed call passes through,
outside the agent's code, where nothing the model reads can reach it.

Two details in :meth:`ToolGateway.dispatch` carry the weight. The denial
reports ``retryable=False``, so the model observes a permanent refusal and
does not spend its remaining turns retrying a wall. And the token is
fetched *after* the decision, so a denied call never causes a credential to
exist.
"""

from __future__ import annotations

from typing import Any

from authz_server import AuthorizationServer, TokenError
from broker import TOOL_AUTHORITY, TokenBroker
from northstar_contracts import (
    EventLog,
    ToolCall,
    ToolResult,
)
from northstar_policy import Decision, PolicyEngine, Principal
from northstar_runtime import ToolRegistry

__all__ = ["DecisionLog", "TokenBoundTools", "ToolGateway"]


class DecisionLog:
    """A policy engine that records every decision it makes.

    Wrapping the engine rather than the gateway is deliberate: the audit
    record is produced by the component that produced the decision, so
    there is no path that decides without recording. "Why was this
    denied?" is answerable from the log without reading the policy source,
    because the rule name is in the record.

    Args:
        engine: The decision point to delegate to.
        events: Where to append the record.
    """

    def __init__(self, engine: PolicyEngine, events: EventLog) -> None:
        self.engine = engine
        self.events = events

    def evaluate(
        self,
        principal: Principal,
        call: ToolCall,
        ctx: dict[str, Any],
    ) -> Decision:
        """Decide, record, and return the decision."""
        verbose = getattr(self.engine, "evaluate_verbose", None)
        if verbose is None:
            decision = self.engine.evaluate(principal, call, ctx)
            rule, reason = "unknown", ""
        else:
            verdict = verbose(principal, call, ctx)
            decision, rule, reason = (
                verdict.decision,
                verdict.rule,
                verdict.reason,
            )
        audience, scope = TOOL_AUTHORITY.get(call.name, ("", ""))
        # The event log's type set is closed, so a policy decision rides on
        # ``tool.called``: it is the record of a call being attempted, and
        # the decision is what happened to the attempt.
        self.events.emit(
            str(ctx.get("run_id", "")),
            int(ctx.get("step", 0)),
            "tool.called",
            {
                # The eight fields the chapter asks for, minus the ones
                # this artifact has no sub-agent or approver for.
                "user_id": principal.user_id,
                "agent_id": principal.agent_id,
                "operator_id": principal.operator_id,
                "tool": call.name,
                "arguments": dict(call.arguments),
                "decision": decision.value,
                "rule": rule,
                "reason": reason,
                "audience": audience,
                "scope_required": scope,
                "scopes_granted": sorted(principal.scopes),
            },
        )
        return decision


class TokenBoundTools:
    """The tool surface, reachable only with a token minted for it.

    Every tool sits behind an audience. Presenting a refund token to the
    message service fails here, in the receiver, which is where an
    audience check has to happen for it to mean anything.

    Args:
        registry: The real tool implementations.
        server: Verifies the token it is handed.
    """

    def __init__(
        self,
        registry: ToolRegistry,
        server: AuthorizationServer,
    ) -> None:
        self.registry = registry
        self.server = server
        #: Tools that ran, with the audience each ran under.
        self.accepted: list[tuple[str, str]] = []

    def dispatch(self, call: ToolCall, token: Any) -> ToolResult:
        """Verify the token, then run the tool.

        A token failure is a permanent failure. Reporting it as retryable
        would send the agent round the loop against a wall it cannot get
        past, which is the behaviour the ``retryable`` flag exists to
        prevent.
        """
        audience, scope = TOOL_AUTHORITY.get(call.name, ("", ""))
        try:
            self.server.verify(token, audience=audience, scope=scope)
        except TokenError as exc:
            return ToolResult.failure(
                call.id,
                f"{type(exc).__name__}: {exc}",
                retryable=False,
            )
        self.accepted.append((call.name, audience))
        return self.registry.dispatch(call)


class ToolGateway:
    """The one place a proposed tool call becomes an outbound request.

    Args:
        policy: The decision point. Wrap it in :class:`DecisionLog` so the
            audit record cannot be skipped.
        broker: Mints the just-in-time credential.
        tools: The token-bound tool surface.
    """

    def __init__(
        self,
        policy: DecisionLog | PolicyEngine,
        broker: TokenBroker,
        tools: TokenBoundTools,
    ) -> None:
        self.policy = policy
        self.broker = broker
        self.tools = tools

    def dispatch(self, principal: Principal, call: ToolCall,
                 ctx: dict[str, Any]) -> ToolResult:
        """Every governed call passes here. No bypass path."""
        if self.policy.evaluate(principal, call, ctx) is not Decision.ALLOW:
            return ToolResult(call.id, ok=False, content={
                "error": "not_authorized", "retryable": False,
            })
        token = self.broker.for_call(principal, call)  # scoped, 60s
        return self.tools.dispatch(call, token=token)
