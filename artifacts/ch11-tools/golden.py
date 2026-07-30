"""The golden trajectory, and the gate the four-line diff has to fail.

The opening incident was a description edit. Everything a normal review looks
at was green: the unit tests called ``search_orders`` and asserted on the rows
it returned and passed identically before and after; the JSON Schema was
byte-for-byte unchanged; every tool call in every trace was well-formed. The
one thing that changed was a sentence no human reads at runtime and the model
reads on every single turn.

So the gate cannot be a schema test and cannot be an argument test. It has to
be a *trajectory* test: for a damaged-item ticket, ``get_order`` is called
before ``issue_refund``, and the refund lands on the item rather than on the
order.

**What the mock can and cannot do, stated plainly.** ``FakeModel`` is scripted
per goal; a scripted model cannot read a description, so it cannot reproduce
the incident on its own. :class:`ReadsTheDescription` is a deterministic
stand-in that *does* read the ``search_orders`` description it is handed, and
branches on whether that description still says the rows are partial and says
what to call next. That makes the description causal in this artifact, which
is the property the gate needs to be worth anything. It is emphatically not a
claim about how a real model reads prose. What is being demonstrated is that
the trajectory gate fires on the trajectory change; whether a given model
makes that trajectory change against a given description is an empirical
question only an eval against that model settles.
"""

from __future__ import annotations

from typing import Any

from northstar_contracts import (
    Message,
    RunState,
    ToolCall,
    ToolSpec,
    estimate_tokens,
)
from northstar_evals import StateGrader, TrajectoryGrader, trajectory
from northstar_runtime import ModelResponse

__all__ = [
    "DAMAGED_TICKET",
    "GOLDEN_TRAJECTORY",
    "LAMP_SHADE_CENTS",
    "ORDER",
    "ORDER_TOTAL_CENTS",
    "ReadsTheDescription",
    "outcome_gate",
    "trajectory_gate",
]

#: The order from the incident: 8,400 cents in total, of which the lamp shade
#: is 3,250.
ORDER = "NR-2026-0041827"
ORDER_TOTAL_CENTS = 8400
LAMP_SHADE_CENTS = 3250

#: The ticket. One cracked lamp shade, not a whole order.
DAMAGED_TICKET = (
    f"Customer says the lamp shade in order {ORDER} arrived cracked. "
    "Refund the damaged item."
)

#: The path a correct run takes. Other calls may appear between these; a run
#: that reads the policy twice is following a valid alternative, and a gate
#: that insists on an exact sequence rejects every one of them.
GOLDEN_TRAJECTORY: tuple[str, ...] = (
    "search_orders",
    "get_order",
    "preview_refund",
    "issue_refund",
)

#: Phrases that make the description do its job. The first says the rows are
#: partial; the second says what to call next. Both were deleted by the diff.
_PARTIAL_HINTS: tuple[str, ...] = ("summaries", "no item-level")
_NEXT_CALL_HINT = "get_order"


def description_is_complete(spec: ToolSpec) -> bool:
    """Whether ``search_orders``' description still does its two jobs.

    Args:
        spec: The ``search_orders`` contract as the model was handed it.

    Returns:
        ``True`` when the description says the rows are partial *and* names
        what to call next. Either sentence missing is enough: knowing the rows
        are incomplete without knowing the remedy leaves the model guessing,
        and naming ``get_order`` without saying why does not tell it when.
    """
    text = spec.description.lower()
    says_partial = any(hint in text for hint in _PARTIAL_HINTS)
    says_next = _NEXT_CALL_HINT in text
    return says_partial and says_next


class ReadsTheDescription:
    """A deterministic stand-in for the model that reads one description.

    Satisfies :class:`northstar_runtime.providers.ModelProvider`. Given the
    damaged-item ticket it searches, then branches on the ``search_orders``
    description it was handed:

    * complete description -- it calls ``get_order``, sees the item lines,
      previews 3,250 cents for the lamp shade, and refunds that;
    * drifted description -- it reads "returns matching orders", sees a status
      and a total on every row, concludes it has what it needs, and refunds
      the total.

    Attributes:
        read_description_as: Whichever branch was taken, for the report.
    """

    def __init__(self, order_id: str = ORDER) -> None:
        self.order_id = order_id
        self.read_description_as = ""
        self.model = "reads-the-description-1"

    def complete(
        self,
        messages: list[Message],
        tools: list[ToolSpec],
    ) -> ModelResponse:
        """Return the next turn, given the tools as they are described."""
        turn = sum(1 for m in messages if m.role == "assistant")
        search = _spec_named(tools, "search_orders")
        complete_description = search is not None and description_is_complete(
            search
        )
        self.read_description_as = (
            "partial rows, call get_order next"
            if complete_description
            else "complete rows, nothing further to call"
        )
        script = (
            self._careful_script()
            if complete_description
            else self._hasty_script()
        )
        step = script[turn] if turn < len(script) else "Nothing further."
        return self._respond(messages, tools, step)

    def _careful_script(self) -> list[Any]:
        """What a model does when the description says the rows are partial."""
        return [
            ToolCall(
                "g1",
                "search_orders",
                {"customer_id": "CUST-8841", "page_size": 5},
            ),
            ToolCall("g2", "get_order", {"order_id": self.order_id}),
            ToolCall("g3", "get_policy", {"reason": "damaged"}),
            ToolCall(
                "g4",
                "preview_refund",
                {
                    "order_id": self.order_id,
                    "amount_cents": LAMP_SHADE_CENTS,
                    "reason": "damaged",
                },
            ),
            ToolCall(
                "g5",
                "issue_refund",
                {
                    "order_id": self.order_id,
                    "amount_cents": LAMP_SHADE_CENTS,
                    "reason": "damaged",
                },
            ),
            f"Refunded {LAMP_SHADE_CENTS} cents for the damaged lamp shade.",
        ]

    def _hasty_script(self) -> list[Any]:
        """What the four-line diff produced: the total, from the search row."""
        return [
            ToolCall(
                "d1",
                "search_orders",
                {"customer_id": "CUST-8841", "page_size": 5},
            ),
            ToolCall(
                "d2",
                "issue_refund",
                {
                    "order_id": self.order_id,
                    "amount_cents": ORDER_TOTAL_CENTS,
                    "reason": "damaged",
                },
            ),
            f"Refunded {ORDER_TOTAL_CENTS} cents for order {self.order_id}.",
        ]

    def _respond(
        self,
        messages: list[Message],
        tools: list[ToolSpec],
        step: Any,
    ) -> ModelResponse:
        """Wrap one script step as a provider response.

        The input-token count includes the tool definitions, because they are
        part of the prompt on every single turn. Thirty tools at 400 tokens
        each is 12,000 tokens of standing overhead before the conversation
        starts, and a description edit changes that number too.
        """
        input_tokens = sum(
            estimate_tokens(m.content) + 3 for m in messages
        ) + sum(estimate_tokens(t.to_dict()) for t in tools)
        if isinstance(step, str):
            return ModelResponse(
                text=step,
                input_tokens=input_tokens,
                output_tokens=estimate_tokens(step),
                model=self.model,
                stop_reason="end_turn",
            )
        return ModelResponse(
            tool_calls=[step],
            input_tokens=input_tokens,
            output_tokens=estimate_tokens(step.to_dict()),
            model=self.model,
            stop_reason="tool_use",
        )


def _spec_named(tools: list[ToolSpec], name: str) -> ToolSpec | None:
    """One spec out of the list the model was handed."""
    for spec in tools:
        if spec.name == name:
            return spec
    return None


def trajectory_gate() -> TrajectoryGrader:
    """The gate the description diff has to fail.

    ``before=[("get_order", "issue_refund")]`` is the whole check: read the
    item-level record before you move money against a single item.
    ``run_code`` is forbidden because a refund never needs it, and a run that
    reaches for it is a run that lost the thread.
    """
    return TrajectoryGrader(
        required=["search_orders", "get_order", "issue_refund"],
        forbidden=["run_code"],
        before=[
            ("get_order", "issue_refund"),
            ("preview_refund", "issue_refund"),
            ("get_policy", "issue_refund"),
        ],
        max_calls=8,
        max_repeats=2,
    )


def outcome_gate() -> StateGrader:
    """What the world must look like afterwards.

    The trajectory gate catches the wrong *path*; this catches the wrong
    *amount*. A run that read the order and then refunded the total anyway
    passes the first and fails this one, which is why the chapter wants both.
    """
    return (
        StateGrader()
        .refunded(ORDER, LAMP_SHADE_CENTS)
        .no_duplicate_refunds(ORDER)
    )


def path_of(run: RunState) -> list[str]:
    """The run's tool sequence. Recovered from the messages, not tracked."""
    return trajectory(run)
