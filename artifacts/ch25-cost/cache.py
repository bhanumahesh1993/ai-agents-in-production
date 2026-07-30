"""A prefix-stable prompt assembler and a tenant-scoped prefix cache.

Two ideas, and only one of them is about money.

**Prefix stability.** A provider caches a *prefix*: if the first N tokens of
this request are byte-identical to a recent one, they can be served from
cache. An agent loop is nearly the ideal workload for that, because turn
five's context is turn four's context with new material at the end — as
long as nothing at the front moves. Put a run id or a timestamp in the
system prompt and the discount is gone, silently.

**Tenant scoping.** The cache key is scoped by tenant *first* and content
second. A cache keyed on content alone is not a performance bug with a
privacy footnote; it is a cross-tenant disclosure with a performance
benefit. :func:`cache_key` makes the tenant part of the hashed payload, so
there is no key a second tenant can compute that collides with the first.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field

from northstar_contracts import Message, ToolSpec, content_hash, estimate_tokens

__all__ = [
    "Prompt",
    "PrefixCache",
    "assemble",
    "cache_key",
    "prefix_of",
]


def cache_key(tenant: str, prefix: Sequence[Message]) -> str:
    """Scope first, then content. Never content alone."""
    return content_hash(
        {"tenant": tenant, "prefix": [m.to_dict() for m in prefix]}
    )


def prefix_of(
    messages: Sequence[Message],
    tools: Sequence[ToolSpec],
) -> list[Message]:
    """The stable head of one request: system material, then tool defs.

    Tool definitions are part of the prompt on every turn, so they belong
    in the prefix. They are rendered as one message here purely so that a
    prefix is uniformly a list of messages and can be hashed by
    :func:`cache_key` without a special case.

    The tool list is sorted by name. An unordered set iterated into the
    prompt is the least obvious of the four ways to break your own cache.
    """
    head = [m for m in messages if m.role == "system"]
    if tools:
        head.append(
            Message(
                role="system",
                content=[
                    spec.to_dict()
                    for spec in sorted(tools, key=lambda s: s.name)
                ],
            )
        )
    return head


@dataclass(frozen=True)
class Prompt:
    """One assembled request, split at the point the cache cares about."""

    prefix: list[Message]
    tail: list[Message]

    def messages(self) -> list[Message]:
        """The whole conversation, prefix first."""
        return [*self.prefix, *self.tail]

    @property
    def prefix_tokens(self) -> int:
        """Estimated tokens in the cacheable head."""
        return sum(estimate_tokens(m.content) for m in self.prefix)

    @property
    def tail_tokens(self) -> int:
        """Estimated tokens in the per-run tail."""
        return sum(estimate_tokens(m.content) for m in self.tail)


def assemble(
    system_prompt: str,
    tools: Sequence[ToolSpec],
    conversation: Sequence[Message],
    *,
    reference: str = "",
    retrieval: Sequence[Message] = (),
    run_marker: str = "",
) -> Prompt:
    """Order the context deliberately: stable material first.

    The order is system prompt, tool definitions, long-lived reference
    material, conversation, then the newest retrieval. Everything that
    varies per run goes as late as it can.

    Args:
        system_prompt: The one instruction block every run shares.
        tools: Tool contracts. Sorted by name before hashing.
        conversation: The run's messages so far.
        reference: Long-lived material that changes per release, not per
            run. Still part of the prefix.
        retrieval: Just-in-time material for *this* turn. Never part of
            the prefix, however tempting the ordering looks.
        run_marker: Per-run text deliberately placed at the *front*. Leave
            it empty for the stable assembly; set it to reproduce the
            mistake — a run id or timestamp in the system prompt — and
            watch the hit rate go to zero.

    Returns:
        A :class:`Prompt` split into the cacheable prefix and the tail.
    """
    head = [Message(role="system", content=system_prompt)]
    if run_marker:
        head.insert(0, Message(role="system", content=run_marker))
    if tools:
        head.append(
            Message(
                role="system",
                content=[
                    spec.to_dict()
                    for spec in sorted(tools, key=lambda s: s.name)
                ],
            )
        )
    if reference:
        head.append(Message(role="system", content=reference))
    return Prompt(prefix=head, tail=[*conversation, *retrieval])


@dataclass
class PrefixCache:
    """A tenant-scoped prompt-prefix cache, with its own hit accounting.

    This models what a provider does rather than reimplementing it: it
    records which prefixes have been seen for which tenant and reports how
    many tokens would have been served from cache. The billing side reads
    :attr:`cached_tokens` and prices those tokens at the cache-read rate.

    Attributes:
        hits: Requests whose whole prefix was already warm.
        misses: Requests that had to pay full price for the prefix.
        cached_tokens: Total prefix tokens served warm.
    """

    hits: int = 0
    misses: int = 0
    cached_tokens: int = 0
    _seen: dict[str, int] = field(default_factory=dict)

    def lookup(self, tenant: str, prefix: Sequence[Message]) -> int:
        """Return the number of prefix tokens served from cache.

        A miss warms the entry and returns zero, which is the honest
        answer: the first request pays for everything.
        """
        key = cache_key(tenant, prefix)
        tokens = sum(estimate_tokens(m.content) for m in prefix)
        if key in self._seen:
            self.hits += 1
            self.cached_tokens += tokens
            return tokens
        self._seen[key] = tokens
        self.misses += 1
        return 0

    @property
    def requests(self) -> int:
        """Every lookup, warm or cold."""
        return self.hits + self.misses

    @property
    def hit_rate(self) -> float:
        """Fraction of lookups that were warm. Computed, not asserted."""
        return self.hits / self.requests if self.requests else 0.0

    @property
    def entries(self) -> int:
        """Distinct (tenant, prefix) pairs held."""
        return len(self._seen)
