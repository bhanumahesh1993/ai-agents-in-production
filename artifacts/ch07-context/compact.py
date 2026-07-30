"""Compaction middleware, in two versions, one of which is wrong.

:func:`compact` is the repaired compactor: it prepends a block of facts
computed from the event log before it hands anything to a summariser.
:func:`naive_compact` is the same code with that one statement removed,
which is the compactor Northstar shipped and the one the opening incident
ran under. Everything else about them is identical, which is the point: the
difference between the incident and the non-event is nine lines, and none
of them make the summary better.

Three properties are load-bearing and each is one statement.

**Idempotent.** The middleware runs before every model call, so a
compactor that re-summarises its own summaries degrades a run the way a
photocopy of a photocopy degrades a page. Running :func:`compact` twice on
the same state returns the same messages.

**Boundary-aligned.** :func:`align_boundary` moves the split so it never
lands between an assistant message carrying a tool call and the tool
message carrying that call's result. A message list split there is
malformed for most providers, and the symptom is an intermittent provider
error that correlates with long runs and nothing else.

**Pageable.** :func:`summarize` records the step span the summary covers,
so an agent that needs a detail it no longer holds can call
``get_run_history(from_step, to_step)`` for the original. Compaction
without a retrieval path is amnesia; compaction with one is paging.
"""

from __future__ import annotations

from collections.abc import Sequence

from budget import ContextBudget, fits
from northstar_contracts import EventLog, Message, RunState, ToolSpec
from northstar_runtime import ModelProvider, ModelResponse
from pinned import (
    PINNED_HEADER,
    SUMMARY_HEADER,
    goal_facts,
    ledger_events,
    pinned_facts,
)

__all__ = [
    "KEEP_RECENT",
    "Summarizer",
    "align_boundary",
    "compact",
    "naive_compact",
    "pinned_block",
    "span_of",
    "summarize",
]

#: How many trailing messages survive a compaction event untouched. Small
#: enough that compaction actually reclaims something, large enough that
#: the current sub-task stays whole.
KEEP_RECENT = 8


class Summarizer:
    """A deterministic stand-in for a genuinely capable summariser.

    The chapter's argument only works if the summary is *good*. A
    strawman that mangles the transcript would let a reader conclude the
    fix is a better model. So this one is accurate: it names the order id
    verbatim, reports the customer's complaint, reports the policy
    verdict, and reports the tone.

    It also drops the side-effect ledger, because a committed refund that
    has stopped being conversationally interesting is exactly the detail a
    summary optimising for readability drops. Nothing here is a bug. That
    is the uncomfortable part.
    """

    model = "deterministic-summarizer-1"

    def complete(
        self,
        messages: list[Message],
        tools: list[ToolSpec],
    ) -> ModelResponse:
        """Return the summary as an ordinary model response."""
        return ModelResponse(
            text=self.summarise(messages),
            input_tokens=sum(len(str(m.content)) // 4 for m in messages),
            output_tokens=64,
            model=self.model,
            stop_reason="end_turn",
        )

    @staticmethod
    def summarise(messages: Sequence[Message]) -> str:
        """Compose the prose. Faithful about dialogue, silent on effects."""
        orders = sorted(
            {
                str(m.content["content"].get("order_id"))
                for m in messages
                if m.role == "tool"
                and isinstance(m.content, dict)
                and m.content.get("tool") == "get_order"
                and isinstance(m.content.get("content"), dict)
                and m.content["content"].get("order_id")
            }
        )
        read_policy = any(
            m.role == "tool"
            and isinstance(m.content, dict)
            and m.content.get("tool") == "get_policy"
            for m in messages
        )
        subject = orders[0] if orders else "the customer's order"
        parts = [
            f"The customer contacted support about order {subject}, "
            "a two-item order that has been delivered. One item, a lamp "
            "shade, arrived cracked. The customer has described the "
            "damage in detail and is asking about next steps."
        ]
        if read_policy:
            parts.append(
                "Policy for this SKU and reason permits a partial refund."
            )
        parts.append(
            "The customer has been polite and is waiting on a resolution."
        )
        return " ".join(parts)


def align_boundary(messages: Sequence[Message], index: int) -> int:
    """Move ``index`` to the nearest earlier complete-turn boundary.

    A tool observation whose assistant message sits before the split is an
    orphan: the provider sees a result for a call it was never shown. So
    the split walks backwards past any leading tool messages until it lands
    on the assistant message that requested them, and never before the
    system prompt and the goal.

    Args:
        messages: The full message list.
        index: Desired split point. Negative indexes count from the end,
            so ``-KEEP_RECENT`` means "keep the last KEEP_RECENT".

    Returns:
        An index in ``[2, len(messages)]`` safe to split at.
    """
    count = len(messages)
    split = index if index >= 0 else max(0, count + index)
    split = max(0, min(split, count))
    while 0 < split < count and messages[split].role == "tool":
        split -= 1
    # Never compact away the system prompt or the goal.
    return max(2, min(split, count))


def span_of(older: Sequence[Message], offset: int = 0) -> tuple[int, int]:
    """The step range a block of messages covers, for the history tool."""
    return (offset, offset + max(0, len(older) - 1))


def summarize(
    older: Sequence[Message],
    model: ModelProvider,
    span: tuple[int, int],
) -> str:
    """Summarise a span of history, carrying its step range with it.

    The step range is not decoration. It is the pointer that turns
    compaction into paging: an agent that needs a detail the summary
    dropped calls ``get_run_history`` with these two numbers.
    """
    response = model.complete(list(older), [])
    body = getattr(response, "text", None) or ""
    return (
        f"{SUMMARY_HEADER} (steps {span[0]}-{span[1]}, "
        f"{len(older)} messages replaced). Call "
        f"get_run_history(from_step={span[0]}, to_step={span[1]}) for the "
        f"original.\n{body}"
    )


def pinned_block(events: EventLog) -> str:
    """The verbatim survivors, computed from the log by ordinary code."""
    lines = goal_facts(events) + pinned_facts(ledger_events(events))
    return PINNED_HEADER + "\n".join(lines)


def compact(
    state: RunState,
    budget: ContextBudget,
    model: ModelProvider,
    events: EventLog | None = None,
    specs: Sequence[ToolSpec] = (),
) -> list[Message]:
    """Idempotent: returns messages unchanged when under budget."""
    if fits(state.messages, budget, specs):
        return state.messages
    split = align_boundary(state.messages, -KEEP_RECENT)
    older, keep = state.messages[:split], state.messages[split:]
    log = events if events is not None else EventLog()
    return [
        Message(role="system", content=pinned_block(log)),
        Message(role="system",
                content=summarize(older, model, span=span_of(older))),
        *keep,
    ]


def naive_compact(
    state: RunState,
    budget: ContextBudget,
    model: ModelProvider,
    events: EventLog | None = None,
    specs: Sequence[ToolSpec] = (),
) -> list[Message]:
    """The same compactor with the pinned block removed.

    Northstar's original. It is boundary-aligned, it is idempotent, it
    carries a step span, and it summarises well. It loses money anyway,
    because a good paraphrase of a transcript is not a ledger.
    """
    if fits(state.messages, budget, specs):
        return state.messages
    split = align_boundary(state.messages, -KEEP_RECENT)
    older, keep = state.messages[:split], state.messages[split:]
    return [
        Message(role="system",
                content=summarize(older, model, span=span_of(older))),
        *keep,
    ]
