"""The facts that survive compaction, computed rather than summarised.

The Chapter 7 incident is a genuinely good summary that dropped one fact: a
refund for 3,250 cents had already committed nineteen turns earlier. The
repair is not a better summariser. It is a block of text that never reaches
the summariser at all, because it is *derived* from the run's event log by
ordinary code.

Two rules make that derivation trustworthy.

**The write set comes from the registry, not from a list somebody
maintains.** :data:`WRITE_TOOLS` selects every :class:`ToolSpec` with
``writes=True``, so a new mutating tool is pinned the day it is registered
rather than the day someone remembers.

**Identifiers and amounts are carried verbatim.** A summary that says "the
customer's order" instead of ``NR-2026-0041827``, or "a partial refund"
instead of ``3250``, has destroyed the agent's ability to act on it.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from northstar_contracts import EventLog, ToolSpec, World

__all__ = [
    "PINNED_HEADER",
    "SUMMARY_HEADER",
    "WRITE_TOOLS",
    "goal_facts",
    "ledger_events",
    "pinned_facts",
    "write_tools_of",
]

#: Prefix identifying the pinned block, so the budget accounting and the
#: compactor can both recognise a block they must never summarise again.
PINNED_HEADER = "PINNED (computed from the event log, never summarised):\n"

#: Prefix identifying a summary block, for the same reason.
SUMMARY_HEADER = "SUMMARY of earlier turns"

#: Arguments worth carrying verbatim out of a write call, in this order.
_VERBATIM_ARGS = ("order_id", "amount_cents", "idempotency_key")


def write_tools_of(specs: Sequence[ToolSpec]) -> frozenset[str]:
    """Every mutating tool name in ``specs``.

    Derived, never hand-maintained. A hand-maintained list is a list that is
    correct on the day it is written and wrong on the day a tool is added.
    """
    return frozenset(s.name for s in specs if s.writes)


#: The Northstar write surface, read off the world's own contracts.
WRITE_TOOLS: frozenset[str] = write_tools_of(World().tool_specs())


def ledger_events(log: EventLog) -> list[dict[str, Any]]:
    """Project a run's event log into one record per side effect.

    The loop records a call's *arguments* on ``tool.called`` and its
    *outcome* on ``tool.result``. Neither record alone says "3,250 cents
    left the building against this order", so this joins them by call id and
    keeps only the mutating tools. It is a projection, which matters: the
    event log stays the authoritative append-only record and this is a view
    over it, recomputable at any time.

    Args:
        log: The loop's event log.

    Returns:
        ``tool.result`` records whose payload carries the tool name, whether
        the call landed, and the verbatim identifiers and amount.
    """
    arguments: dict[str, dict[str, Any]] = {}
    out: list[dict[str, Any]] = []
    for record in log.records:
        payload = record["payload"]
        if record["type"] == "tool.called":
            arguments[str(payload.get("call_id"))] = dict(
                payload.get("arguments") or {}
            )
            continue
        if record["type"] != "tool.result":
            continue
        if payload.get("tool") not in WRITE_TOOLS:
            continue
        args = arguments.get(str(payload.get("call_id")), {})
        out.append(
            {
                "type": "tool.result",
                "step": record["step"],
                "payload": {
                    "name": payload["tool"],
                    "ok": bool(payload["ok"]),
                    **{k: args.get(k) for k in _VERBATIM_ARGS},
                },
            }
        )
    return out


def pinned_facts(events: list[dict]) -> list[str]:
    """Verbatim survivors of compaction. Computed, never summarized."""
    out: list[str] = []
    for e in events:
        if e["type"] != "tool.result":
            continue
        p = e["payload"]
        if p["name"] in WRITE_TOOLS and p["ok"]:
            out.append(
                f"COMMITTED {p['name']} order={p['order_id']} "
                f"amount_cents={p['amount_cents']} "
                f"key={p['idempotency_key']} step={e['step']}"
            )
    return out


def goal_facts(log: EventLog) -> list[str]:
    """The run's goal and constraints, pinned from turn zero.

    Read off ``run.started`` rather than off the message list, because the
    message list is the thing compaction rewrites. Goal drift across a
    compaction event is a recognised failure mode and pinning the original
    text costs almost nothing.
    """
    for record in log.records:
        if record["type"] != "run.started":
            continue
        payload = record["payload"]
        return [
            f"GOAL {payload.get('goal', '')}",
            "CONSTRAINT amounts are integer cents",
            "CONSTRAINT refunds at or above 5000 cents need approval",
        ]
    return []
