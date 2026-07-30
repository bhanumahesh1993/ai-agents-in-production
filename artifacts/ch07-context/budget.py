"""The context window as a budget with named line items.

The chapter's argument in one object. A budget stated as "we compact at
80%" tells you a run tripped; it does not tell you *which* line item blew
up, so it cannot tell you what to fix. Per-category caps do, and
:meth:`ContextBudget.exceeded` returns names rather than a boolean for
exactly that reason.

The categories are the five the chapter names plus the reserve, which is
not content at all: it is the room the model needs for its own output on
the final turn. An agent that fills its window to 99% and then cannot emit
a complete tool call has not saved anything.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from northstar_contracts import Message, ToolSpec, estimate_tokens
from pinned import PINNED_HEADER, SUMMARY_HEADER

__all__ = [
    "CATEGORIES",
    "RETRIEVAL_TOOLS",
    "ContextBudget",
    "account",
    "category_of",
    "fits",
    "total_tokens",
]

#: The line items, in the order the chapter introduces them. ``reserve`` is
#: deliberately absent: nothing is ever accounted *into* it.
CATEGORIES: tuple[str, ...] = (
    "system",
    "tools",
    "pinned",
    "history",
    "retrieved",
)

#: Read tools whose results are knowledge rather than conversation. Policy
#: text fetched once is carried forever unless something says it is
#: retrieved knowledge and prunes it on that basis.
RETRIEVAL_TOOLS: frozenset[str] = frozenset({"get_policy"})


@dataclass(frozen=True)
class ContextBudget:
    """Per-category caps in tokens. Enforced in code, not prose."""

    total: int = 60_000
    system: int = 2_000
    tools: int = 6_000
    pinned: int = 1_500
    history: int = 32_000
    retrieved: int = 10_000
    reserve: int = 8_000      # room for the model's own output

    def exceeded(self, used: dict[str, int]) -> list[str]:
        """Which line items are over, not merely whether we are."""
        return [k for k, v in used.items()
                if v > getattr(self, k, 0)]

    @property
    def content_ceiling(self) -> int:
        """Tokens available for content once the reserve is set aside."""
        return self.total - self.reserve


def category_of(message: Message) -> str:
    """Which budget line item this message is charged to.

    The two interesting cases are both system messages. A pinned block is
    charged to ``pinned`` because it is exempt from compaction and needs
    its own ceiling; a summary block is charged to ``history`` because a
    summary is history, and pretending otherwise hides the fact that
    summaries accumulate across compaction events.
    """
    if message.role == "system":
        text = message.content if isinstance(message.content, str) else ""
        if text.startswith(PINNED_HEADER):
            return "pinned"
        if text.startswith(SUMMARY_HEADER):
            return "history"
        return "system"
    if message.role == "tool" and isinstance(message.content, dict):
        if message.content.get("tool") in RETRIEVAL_TOOLS:
            return "retrieved"
    return "history"


def account(
    messages: Sequence[Message],
    specs: Sequence[ToolSpec] = (),
) -> dict[str, int]:
    """Estimated tokens per line item for one assembled context.

    ``specs`` is the line item teams forget. Thirty tools whose schemas
    average 400 tokens is 12,000 tokens spent before the conversation
    starts, re-sent on every turn of the run, and no amount of history
    compaction offsets it.

    Args:
        messages: The message list as it would be sent to the provider.
        specs: Tool contracts sent alongside it.

    Returns:
        A dict keyed by :data:`CATEGORIES`, every key present.
    """
    used = dict.fromkeys(CATEGORIES, 0)
    used["tools"] = sum(estimate_tokens(s.to_dict()) for s in specs)
    for message in messages:
        used[category_of(message)] += estimate_tokens(message.content)
    return used


def total_tokens(
    messages: Sequence[Message],
    specs: Sequence[ToolSpec] = (),
) -> int:
    """Every line item added up. What a provider actually bills."""
    return sum(account(messages, specs).values())


def fits(
    messages: Sequence[Message],
    budget: ContextBudget,
    specs: Sequence[ToolSpec] = (),
) -> bool:
    """Whether this context is inside every cap and the total ceiling.

    Both halves matter. A run can sit under its total and still be broken,
    because its tool definitions doubled and crowded out history; and it
    can satisfy every per-category cap and still leave the model no room to
    answer, which is what the reserve is for.
    """
    used = account(messages, specs)
    if budget.exceeded(used):
        return False
    return sum(used.values()) <= budget.content_ceiling
