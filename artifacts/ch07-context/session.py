"""The forty-turn Northstar session, and a model that reads its context.

Everything in this file exists to make one thing measurable: whether the
agent, at turn 33, can see that a refund committed at turn 12.

That requires a model whose behaviour is a function of the *visible*
context rather than of the turn number. ``FakeModel`` is scripted by turn
index, and it derives that index by counting assistant messages in the
conversation — which is exactly the right design everywhere else in this
repository and exactly the wrong one here, because a compactor removes
assistant messages and the script would silently rewind. So this chapter
ships :class:`ScriptedSession`, which keeps its own turn counter and lets a
scripted step *inspect* the messages before deciding.

:func:`remembers_refund` is that inspection, and it is the whole
experiment. It looks for evidence of a committed refund in what the model
can actually see: a pinned block that says one landed, or the original
tool observation still sitting in the window. It never consults the world
and it never consults the event log, because the model cannot.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence

from history import make_get_run_history
from northstar_contracts import (
    Message,
    ToolCall,
    ToolSpec,
    World,
    estimate_tokens,
)
from northstar_runtime import ModelResponse, ToolRegistry
from pinned import PINNED_HEADER

__all__ = [
    "AMOUNT",
    "GOAL",
    "ORDER",
    "SECOND_ORDER",
    "SKU",
    "ScriptedSession",
    "SessionStep",
    "long_session",
    "northstar_tools",
    "remembers_refund",
]

#: The order from the chapter's opening. Two items, US$84.00, delivered.
ORDER = "NR-2026-0041827"
#: A different order the customer keeps asking about, which is what makes
#: the session long enough to compact in the first place.
SECOND_ORDER = "NR-2026-0041903"
SKU = "NR-LAMPSHADE-03"
#: The damaged lamp shade, in integer cents. Below the 5,000-cent
#: threshold on purpose: no approval gate stands between the model and a
#: second payout, so the only thing that can stop one is memory.
AMOUNT = 3250
GOAL = (
    "Customer says the lamp shade in order NR-2026-0041827 arrived "
    "cracked, and keeps following up about several other things."
)

#: One scripted turn: a tool call, a final string, or a callable that
#: reads the visible conversation and returns one of those.
SessionStep = (
    ToolCall
    | str
    | Callable[[Sequence[Message]], "ToolCall | str"]
)


def remembers_refund(messages: Sequence[Message]) -> bool:
    """Whether the visible context says a refund already committed.

    Two admissible kinds of evidence, and no others:

    * a pinned block asserting a committed write, which survives
      compaction because it is computed rather than summarised;
    * the original ``issue_refund`` observation, which survives only until
      the compaction boundary passes it.

    A summary that mentions "a partial refund" in prose does not count,
    and that is not a technicality. Prose about a refund is a claim about
    the conversation; a receipt id and an amount in cents is a claim about
    the world.
    """
    for message in messages:
        if message.role == "system" and isinstance(message.content, str):
            if message.content.startswith(PINNED_HEADER):
                if "COMMITTED issue_refund" in message.content:
                    return True
            continue
        if message.role != "tool" or not isinstance(message.content, dict):
            continue
        if message.content.get("tool") != "issue_refund":
            continue
        body = message.content.get("content")
        if isinstance(body, dict) and body.get("refund_id"):
            return True
    return False


def _follow_up(messages: Sequence[Message]) -> ToolCall | str:
    """The customer asks, one more time, when the money comes back.

    This is turn 33 of the chapter's incident, written as code. The model
    reads its context, and what it does next is correct given what it can
    see. That is the uncomfortable part: there is no bad reasoning here to
    fix.
    """
    if remembers_refund(messages):
        return (
            f"Your refund of {AMOUNT} cents for the lamp shade has already "
            "been issued and is on its way back to your card."
        )
    return ToolCall(
        "c-refund-again",
        "issue_refund",
        {"order_id": ORDER, "amount_cents": AMOUNT, "reason": "damaged"},
    )


def _filler(index: int) -> list[SessionStep]:
    """One round of the customer changing the subject.

    Reads only. They cost turns and tokens and change nothing about the
    world, which is precisely what makes them a good way to fill a window
    without contaminating the measurement.
    """
    return [
        ToolCall(f"c-f{index}a", "get_order", {"order_id": SECOND_ORDER}),
        ToolCall(
            f"c-f{index}b",
            "search_orders",
            {"customer_id": "CUST-8841", "page_size": 2},
        ),
        ToolCall(
            f"c-f{index}c",
            "get_policy",
            {"reason": "changed_mind", "sku": "NR-MUG-02"},
        ),
    ]


def long_session(filler_rounds: int = 6) -> list[SessionStep]:
    """The scripted conversation, parameterised by how long it runs.

    The first four turns are the chapter's opening: read the order, check
    the policy, refund the damaged item, tell the customer. Then the
    session wanders for ``filler_rounds`` rounds, and then the customer
    asks about the refund again.

    ``filler_rounds`` is the independent variable of the whole experiment.
    Short sessions never cross the budget, so nothing compacts and nothing
    is forgotten. Long ones do. A test suite whose longest run is six
    turns passes on a compactor that loses the refund ledger.
    """
    steps: list[SessionStep] = [
        ToolCall("c1", "get_order", {"order_id": ORDER}),
        ToolCall("c2", "get_policy", {"reason": "damaged", "sku": SKU}),
        ToolCall(
            "c3",
            "issue_refund",
            {"order_id": ORDER, "amount_cents": AMOUNT, "reason": "damaged"},
        ),
        ToolCall(
            "c4",
            "send_message",
            {
                "order_id": ORDER,
                "body": (
                    "Sorry about the cracked lamp shade. A partial refund "
                    "is on its way."
                ),
            },
        ),
    ]
    for index in range(filler_rounds):
        steps.extend(_filler(index))
    steps.append(_follow_up)
    steps.append("Anything else I can help with?")
    return steps


class ScriptedSession:
    """A deterministic model whose turn index survives compaction.

    Args:
        script: The scripted turns, in order.
        model: Name reported on the response and on the span.

    Raises:
        IndexError: When the loop asks for a turn the script does not
            have. Extending the script to make that go away is usually the
            wrong move: it means the run took a path nobody planned.
    """

    def __init__(
        self,
        script: Sequence[SessionStep],
        model: str = "scripted-session-1",
    ) -> None:
        self.script = list(script)
        self.model = model
        self.turn = 0
        #: Estimated input tokens per turn, so the harness can report what
        #: the run actually paid rather than what it hoped to pay.
        self.input_tokens_per_turn: list[int] = []

    def complete(
        self,
        messages: list[Message],
        tools: list[ToolSpec],
    ) -> ModelResponse:
        """Return the scripted turn, after letting it read the context."""
        if self.turn >= len(self.script):
            raise IndexError(
                f"script has {len(self.script)} turns; the loop asked for "
                f"turn {self.turn + 1}. The run took an unplanned path."
            )
        step = self.script[self.turn]
        self.turn += 1
        if callable(step) and not isinstance(step, ToolCall):
            step = step(messages)

        input_tokens = estimate_tokens(
            [m.to_dict() for m in messages]
        ) + sum(estimate_tokens(s.to_dict()) for s in tools)
        self.input_tokens_per_turn.append(input_tokens)

        if isinstance(step, ToolCall):
            return ModelResponse(
                tool_calls=[step],
                input_tokens=input_tokens,
                output_tokens=estimate_tokens(step.to_dict()),
                model=self.model,
                stop_reason="tool_use",
            )
        return ModelResponse(
            text=step,
            input_tokens=input_tokens,
            output_tokens=estimate_tokens(step),
            model=self.model,
            stop_reason="end_turn",
        )


def northstar_tools(
    world: World,
    events: object | None = None,
) -> ToolRegistry:
    """The Northstar surface, plus the tool that pages back into history.

    ``inject_idempotency_key`` is on, which is worth pausing over. It means
    every refund this agent issues carries a key derived from the run and
    the step — the Chapter 1 repair, fully deployed. It does not help. Step
    12 and step 33 are different steps, so they derive different keys, and
    the refund service does what it is asked. The duplicate this chapter is
    about is not a retry.
    """
    registry = ToolRegistry(inject_idempotency_key=True)
    registry.register_all(world.tools())
    if events is not None:
        spec, fn = make_get_run_history(events)
        registry.register(spec, fn)
    return registry
