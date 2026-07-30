"""The controls that stop both attacks, and none of them is a filter.

Nothing here inspects the payload. There is no classifier, no keyword list, and
no instruction in a prompt asking the model to disregard what it reads. Those are
tripwires: worth deploying for signal, and never the thing standing between
untrusted content and a privileged action, because a control whose failure mode
is "the action executes anyway" is not an authorization control.

What is here is two of the three cuts from the chapter, evaluated outside the
model at the action boundary, on every call.

**Cut the private data.** :class:`ScopedTools` binds ``search_orders`` to the
customer id in the run's principal and filters server-side, so a query for
another buyer's orders returns an empty page rather than a refusal. That
difference is deliberate: a denial teaches an injected instruction to try a
different phrasing, and an empty result teaches it nothing. This is the cheapest
cut and it survives every injection technique, because the data never enters the
context at all.

**Cut the external communication.** Destinations are derived from authoritative
state rather than from the model. Northstar's ``send_message`` takes an
``order_id`` and resolves the recipient from the order record, so the model
cannot name a recipient -- and :class:`ScopeAndEgressPolicy` then only has to
answer whether the run owns that order.

The third cut, quarantining untrusted content so it cannot re-enter as apparent
instruction, is the most architecturally demanding of the three and is not built
here. It is named rather than faked; see the README.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from northstar_contracts import ToolCall, ToolResult, World
from northstar_policy import Decision, Principal
from northstar_runtime import ToolRegistry

__all__ = [
    "ORDER_ARGUMENT_TOOLS",
    "OUTBOUND_TOOLS",
    "ScopeAndEgressPolicy",
    "ScopedTools",
    "owners_of",
]

#: Tools that name one order in their arguments, and therefore need an
#: ownership check before they run.
ORDER_ARGUMENT_TOOLS: frozenset[str] = frozenset(
    {"get_order", "issue_refund", "send_message", "escalate_to_specialist"}
)

#: Tools that reach outside the boundary.
OUTBOUND_TOOLS: frozenset[str] = frozenset(
    {"send_message", "escalate_to_specialist"}
)


def owners_of(world: World) -> dict[str, str]:
    """Resolve order ownership once, at admission.

    The chapter's excerpt writes ``self.world.owner_of(...)``, which reads well
    and is the wrong shape for a decision point: one that has to query the
    system it is protecting fails open when that system is down. So ownership
    is resolved into a map before the run starts, the same way
    ``northstar_policy.flagged_order`` takes its ids as an argument.
    """
    return {
        order_id: str(order["customer_id"])
        for order_id, order in world.orders.items()
    }


class ScopeAndEgressPolicy:
    """Scope and egress rules. The model cannot reach these.

    Args:
        owners: Order id to the customer who owns it, resolved at admission.
        default: What to answer for a call no rule covers. ``REQUIRE_APPROVAL``
            is the honest default for an agent holding a payments credential:
            an unrecognised privileged call is a question for a human, not a
            coin flip.
    """

    def __init__(
        self,
        owners: Mapping[str, str],
        default: Decision = Decision.REQUIRE_APPROVAL,
    ) -> None:
        self.owners = dict(owners)
        self.default = default
        #: Every call this policy refused, in order. The event log records
        #: them too; this is here so a test can read them without parsing.
        self.denied: list[tuple[str, str]] = []
        #: Every call it *saw*, with arguments. A refused call raises before
        #: the loop checkpoints, so this is the only record that the agent
        #: emitted it -- and "the agent still emitted the call" is the fact
        #: the chapter is about.
        self.seen: list[tuple[str, dict[str, Any]]] = []

    def evaluate(
        self,
        principal: Principal,
        call: ToolCall,
        ctx: dict[str, Any],
    ) -> Decision:
        """Return the decision for one proposed call.

        Ownership before anything else. An action bound to a principal and an
        intent the deputy did not derive from untrusted content is the whole
        defence against the confused-deputy pattern, and it does not care how
        persuasive the text that produced the call was.
        """
        decision, _rule, _reason = self._decide(principal, call)
        # Recorded here and not in ``_decide``, because the loop asks for a
        # verdict and then asks again for its reason, and a call the agent
        # emitted once must not appear in the record twice.
        entry = (call.name, dict(call.arguments))
        if entry not in self.seen:
            self.seen.append(entry)
        if decision is Decision.DENY:
            refusal = (call.name, str(call.arguments.get("order_id", "")))
            if refusal not in self.denied:
                self.denied.append(refusal)
        return decision

    def evaluate_verbose(
        self,
        principal: Principal,
        call: ToolCall,
        ctx: dict[str, Any],
    ) -> Any:
        """The decision with a reason, for the approval payload and the log."""
        from northstar_policy import PolicyVerdict

        decision, rule, reason = self._decide(principal, call)
        return PolicyVerdict(decision, rule=rule, reason=reason)

    def _decide(
        self,
        principal: Principal,
        call: ToolCall,
    ) -> tuple[Decision, str, str]:
        """The rule, with no bookkeeping, so it can be asked twice."""
        if call.name in ("get_policy", "search_orders"):
            # Reads that carry no record identity. ``search_orders`` is not
            # gated here at all: the fix is server-side filtering in
            # ScopedTools, not a decision the model can reason about.
            return (
                Decision.ALLOW,
                "read.no_record_identity",
                "narrowed server-side rather than decided here",
            )

        if call.name in ORDER_ARGUMENT_TOOLS:
            order_id = str(call.arguments.get("order_id", ""))
            owner = self.owners.get(order_id)
            if owner is not None and owner == principal.user_id:
                return (
                    Decision.ALLOW,
                    "order.owned_by_principal",
                    f"{order_id} belongs to {principal.user_id}",
                )
            return (
                Decision.DENY,
                "order.owned_by_principal",
                f"{order_id} does not belong to {principal.user_id}",
            )

        return (
            self.default,
            "default",
            "no rule covers this call, so a human decides",
        )


class ScopedTools(ToolRegistry):
    """A registry that narrows what the agent can reach, server-side.

    ``search_orders`` is the interesting one. The model may pass any
    ``customer_id`` it likes; this replaces it with the one in the principal
    before the tool runs. So an out-of-scope query returns an empty page, which
    is not a signal an attacker can iterate against.

    The registry is also where a reader points this harness at their own agent:
    swap the bindings and everything else -- the cases, the scorer, the
    policy -- is unchanged.

    Args:
        base: The registry to wrap.
        principal: Whose scope the reads are bound to.
        scoped: Whether to bind at all. ``False`` is the unprotected
            configuration, and it is the shape Northstar shipped for eleven
            months.
    """

    def __init__(
        self,
        base: ToolRegistry,
        principal: Principal,
        *,
        scoped: bool = True,
    ) -> None:
        super().__init__(
            inject_idempotency_key=True, validate=base.validate
        )
        self.register_all(base.bindings())
        self.principal = principal
        self.scoped = scoped
        #: Calls whose arguments this registry rewrote, in order.
        self.narrowed: list[tuple[str, dict[str, Any]]] = []

    def dispatch(
        self,
        call: ToolCall,
        run_id: str | None = None,
        step: int | None = None,
    ) -> ToolResult:
        """Narrow the arguments if this registry is scoped, then dispatch."""
        if self.scoped and call.name == "search_orders":
            narrowed = {**call.arguments, "customer_id": self.principal.user_id}
            if narrowed != call.arguments:
                self.narrowed.append((call.name, dict(call.arguments)))
            call = ToolCall(id=call.id, name=call.name, arguments=narrowed)
        return super().dispatch(call, run_id=run_id, step=step)
