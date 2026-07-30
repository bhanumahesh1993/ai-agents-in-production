"""The handoff contract, and the one line that decides whether it pays twice.

A handoff is not a message; it is a transfer of responsibility, and a
transfer of responsibility that does not enumerate what is being transferred
is where multi-agent systems fail. Six things must move with control, and
``handoff.yaml`` is the same contract in the shape a config file would take.

The load-bearing part of this module is at the bottom. :func:`refund_key`
derives the idempotency key from the *origin* run and step, carried across
the hop. :func:`refund_key_local` derives it from the receiver's own run,
which is a fresh identity, so a retried step presents a new key and the
customer is paid a second time. The two functions differ by one argument and
by 12,000 cents.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from northstar_contracts import Money, RunState, idempotency_key

__all__ = [
    "CONTRACT_CATEGORIES",
    "BudgetExhausted",
    "Handoff",
    "NotPermitted",
    "load_contract",
    "refund_key",
    "refund_key_local",
]

#: The six things that must move with control, mapped onto the fields that
#: carry them. A test asserts every category is represented, because a
#: contract with five of the six is the one that fails in production.
CONTRACT_CATEGORIES: dict[str, tuple[str, ...]] = {
    "goal": ("goal",),
    "constraints": (
        "allowed_tools",
        "prohibited_tools",
        "approval_threshold_cents",
    ),
    "state_reference": ("evidence_refs",),
    "budget": ("budget_cents_left", "turns_left", "deadline_ts"),
    "provenance": (
        "origin_run_id",
        "origin_step_id",
        "trace_parent",
        "chain",
        "auth_context_ref",
    ),
    "return_contract": ("return_to", "return_schema", "on_timeout"),
}


class NotPermitted(RuntimeError):
    """The receiver tried something the handoff did not transfer."""


class BudgetExhausted(RuntimeError):
    """The remaining budget carried across the hop ran out."""


@dataclass(frozen=True)
class Handoff:
    origin_run_id: str       # anchors every idempotency key
    origin_step_id: int
    goal: str
    allowed_tools: tuple[str, ...]
    prohibited_tools: tuple[str, ...]
    approval_threshold_cents: Money
    budget_cents_left: Money
    turns_left: int
    return_to: str
    deadline_ts: float
    # Everything below is part of the same contract; the chapter prints the
    # ten fields above because they are the ones that change behaviour in
    # this artifact.
    from_agent: str = "support-orchestrator@3.2.1"
    to_agent: str = "fraud-review@1.8.0"
    evidence_refs: tuple[str, ...] = ()
    trace_parent: str = "00-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-01"
    auth_context_ref: str = "delegation://northstar/fraud-review"
    return_schema: str = "fraud_verdict.v1"
    on_timeout: str = "return_partial_and_escalate"
    chain: tuple[str, ...] = field(default=())

    def permits(self, tool: str) -> bool:
        """Whether the receiver may call ``tool`` under this transfer."""
        if tool in self.prohibited_tools:
            return False
        return tool in self.allowed_tools

    def require(self, tool: str) -> None:
        """Raise unless the transfer carried permission for ``tool``.

        Enforced at the boundary rather than asserted in a prompt, because a
        receiver's effective permissions must be a subset of the sender's
        and a prompt is not a subset of anything.
        """
        if not self.permits(tool):
            raise NotPermitted(
                f"{self.to_agent} may not call {tool!r}: allowed="
                f"{list(self.allowed_tools)} prohibited="
                f"{list(self.prohibited_tools)}"
            )

    def narrow(
        self,
        to_agent: str,
        goal: str,
        allowed_tools: tuple[str, ...],
        *,
        spend_cents: Money = 0,
        spend_turns: int = 0,
    ) -> Handoff:
        """Derive the next hop's contract from this one.

        Budgets are remainders, never fresh allowances: a three-hop chain
        that resets the counter spends three full budgets while every
        individual agent reports staying within limits. And permissions can
        only shrink, which is what stops the 5,000-cent threshold
        evaporating one hop away from the agent written to respect it.
        """
        widened = set(allowed_tools) - set(self.allowed_tools)
        if widened:
            raise NotPermitted(
                f"a hop cannot widen permissions; {sorted(widened)} were not "
                f"granted to {self.to_agent}"
            )
        cents = self.budget_cents_left - spend_cents
        turns = self.turns_left - spend_turns
        if cents <= 0 or turns <= 0:
            raise BudgetExhausted(
                f"nothing left to transfer: {cents}c, {turns} turn(s)"
            )
        return replace(
            self,
            from_agent=self.to_agent,
            to_agent=to_agent,
            goal=goal,
            allowed_tools=tuple(allowed_tools),
            budget_cents_left=cents,
            turns_left=turns,
            chain=(*self.chain, self.to_agent),
        )

    def to_dict(self) -> dict[str, Any]:
        """Field by field, which is how the demo prints it."""
        return {
            "origin_run_id": self.origin_run_id,
            "origin_step_id": self.origin_step_id,
            "from_agent": self.from_agent,
            "to_agent": self.to_agent,
            "goal": self.goal,
            "evidence_refs": list(self.evidence_refs),
            "allowed_tools": list(self.allowed_tools),
            "prohibited_tools": list(self.prohibited_tools),
            "approval_threshold_cents": self.approval_threshold_cents,
            "budget_cents_left": self.budget_cents_left,
            "turns_left": self.turns_left,
            "deadline_ts": self.deadline_ts,
            "trace_parent": self.trace_parent,
            "auth_context_ref": self.auth_context_ref,
            "return_to": self.return_to,
            "return_schema": self.return_schema,
            "on_timeout": self.on_timeout,
            "chain": list(self.chain),
        }


def refund_key(h: Handoff) -> str:
    """Derive from the ORIGIN run, carried across the hop."""
    return idempotency_key(h.origin_run_id, h.origin_step_id)


def refund_key_local(state: RunState) -> str:
    """Broken variant: the receiver has a new run id, so a
    retried step presents a new key and pays a second time."""
    return idempotency_key(state.run_id, state.step)


_KEY_RE = re.compile(r"^\s*([a-z_]+):\s*(.*?)\s*(?:#.*)?$")


def load_contract(path: str | Path) -> dict[str, str]:
    """Read ``handoff.yaml`` without a YAML dependency.

    Enough to check that the printed contract and the typed one carry the
    same fields. A real deployment parses this with a schema; the point here
    is that the file and the dataclass do not drift apart unnoticed.
    """
    fields: dict[str, str] = {}
    for line in Path(path).read_text().splitlines():
        if not line.strip() or line.strip().startswith("#"):
            continue
        match = _KEY_RE.match(line)
        if match and match.group(2):
            fields[match.group(1)] = match.group(2).strip("\"'")
    return fields
