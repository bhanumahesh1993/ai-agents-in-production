"""The just-in-time credential broker.

The agent never holds a long-lived credential and never sees a raw
delegated token. It holds a :class:`~northstar_policy.Principal`, and when
a governed call is about to be dispatched the broker exchanges the user's
token for a credential that names one audience, one scope, and one
resource, and lives for sixty seconds.

Three properties follow from doing it here rather than at process start:

* a denied call never causes a credential to exist, because the gateway
  asks the policy first and the broker second;
* the credential's lifetime is shorter than the interval between
  checkpoints, so a leaked checkpoint contains nothing usable;
* the scope is the one the call needs, not the union of every scope the
  agent might ever need.
"""

from __future__ import annotations

from typing import Any

from northstar_contracts import ToolCall
from northstar_policy import Principal

from authz_server import (
    DEFAULT_TTL_SECONDS,
    AuthorizationServer,
    InsufficientScope,
    ScopedToken,
    actor_token_subject,
)

__all__ = [
    "AGENT_VERSION",
    "TOOL_AUTHORITY",
    "TokenBroker",
    "actor_token_for",
    "resource_for",
    "subject_token_for",
]

#: The build of the agent. It goes in the actor claim, which is what makes
#: "which agent version issued this refund" answerable from the refund
#: service's own logs.
AGENT_VERSION = "v1.8.0"

#: Which audience and scope each Northstar tool needs. This is the table
#: that turns ``refunds.*`` into ``refunds.write`` against one service:
#: a token minted for the refund service is refused by the message
#: service, so an agent talked into calling the wrong endpoint gets
#: nothing.
TOOL_AUTHORITY: dict[str, tuple[str, str]] = {
    "get_order": ("https://orders.northstar.example", "orders.read"),
    "get_policy": ("https://orders.northstar.example", "orders.read"),
    "search_orders": ("https://orders.northstar.example", "orders.read"),
    "issue_refund": ("https://refunds.northstar.example", "refunds.write"),
    "send_message": ("https://messages.northstar.example", "messages.send"),
    "escalate_to_specialist": (
        "https://cases.northstar.example",
        "cases.write",
    ),
}


def subject_token_for(principal: Principal) -> str:
    """The user's token, as the exchange's subject.

    A background agent with no requesting user has no subject token, and
    the correct answer is to say so rather than to substitute the service
    account. That substitution is the privilege escalation the chapter
    warns about, and it is always introduced in an error handler.
    """
    if principal.user_id is None:
        raise InsufficientScope("no subject: this run has no user")
    return f"user:{principal.user_id}"


def actor_token_for(principal: Principal) -> str:
    """The agent's own workload credential, as the exchange's actor."""
    return f"workload:{principal.agent_id}@{AGENT_VERSION}"


def resource_for(call: ToolCall) -> str:
    """The narrowest resource the call names.

    ``refunds.write`` bounded to one order is a materially different grant
    from ``refunds.write`` across the tenant, and this is the line that
    makes the former possible.
    """
    order_id = call.arguments.get("order_id")
    if isinstance(order_id, str) and order_id:
        return f"order:{order_id}"
    return "tenant:northstar"


class TokenBroker:
    """Exchanges a user's token for a scoped, short-lived, bound one.

    Args:
        server: The authorization server to exchange against.
        ttl_s: Lifetime of every minted credential, in seconds.
    """

    def __init__(
        self,
        server: AuthorizationServer,
        ttl_s: int = DEFAULT_TTL_SECONDS,
    ) -> None:
        self.server = server
        self.ttl_s = ttl_s
        #: Append-only record of every exchange, for the audit trail.
        self.exchanges: list[dict[str, Any]] = []

    # -- the three facts the exchange needs, from the server ---------------

    def grants_for(self, subject_token: str) -> frozenset[str]:
        """Scopes the subject holds. The server decides, not the agent."""
        return self.server.grants_for(subject_token)

    def subject_of(self, subject_token: str) -> str:
        """The user id inside a subject token."""
        return self.server.subject_of(subject_token)

    def sign(self, **claims: Any) -> ScopedToken:
        """Mint the token. Delegated to the authorization server."""
        return self.server.sign(**claims)

    # -- RFC 8693 ---------------------------------------------------------

    def exchange(self, subject_token: str, actor_token: str,
                 audience: str, scope: str,
                 resource: str, ttl_s: int = 60) -> ScopedToken:
        """Mint a delegated token. Read-only: safe to retry."""
        if scope not in self.grants_for(subject_token):
            raise InsufficientScope(scope)   # step up, never widen
        return self.sign(
            sub=self.subject_of(subject_token),
            act={"sub": actor_token_subject(actor_token)},
            aud=audience, scope=scope, resource=resource,
            ttl_s=ttl_s,
        )

    def for_call(self, principal: Principal, call: ToolCall) -> ScopedToken:
        """Acquire the one credential this one call needs.

        Raises:
            InsufficientScope: The user's grant does not cover the scope
                the tool requires, or the tool is unknown. An unknown tool
                fails closed: there is no default audience.
        """
        authority = TOOL_AUTHORITY.get(call.name)
        if authority is None:
            raise InsufficientScope(f"unknown tool {call.name!r}")
        audience, scope = authority
        token = self.exchange(
            subject_token_for(principal),
            actor_token_for(principal),
            audience=audience,
            scope=scope,
            resource=resource_for(call),
            ttl_s=self.ttl_s,
        )
        self.exchanges.append(
            {
                "tool": call.name,
                "audience": audience,
                "scope": scope,
                "resource": token.claims["resource"],
                # The reference, never the value. A log line holding a
                # credential is a credential in your log retention.
                "token_ref": token.value,
            }
        )
        return token
