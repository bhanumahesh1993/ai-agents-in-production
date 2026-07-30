"""One Northstar task, one scripted model, and the meter every build shares.

Every pattern in this chapter is an edit to the same loop on the same
ticket, so the fixture lives here and the six builds import it. Anything a
pattern needs that is not in this module is the pattern's own cost.

The task is ticket 8812: order ``NR-2026-0041827``, the cracked lamp shade
at 3,250 cents, reason ``damaged``, refundable in full under the shipped
policy and below the 5,000-cent approval threshold.

The claim is a *line item* on a larger order, and that is load-bearing.
The second half of the demo needs the injected timeout to leave two refund
rows in the ledger, which the world's own over-refund guard would prevent on
an order whose total equalled the claim: the guard, not the missing
idempotency key, would be what stopped the duplicate, and then the chapter
would be measuring the wrong thing.

Two measurement notes, because a cost comparison that misrepresents its own
units is worse than no comparison.

**Tokens** come from :class:`Meter`, which counts what the provider reported
for every model call any part of a pattern made — inside the loop or
outside it. A router's classification call and a critic's review call are
model calls you pay for, and a count taken from the finished ``RunState``
would miss both.

**Latency in mock mode is not latency.** ``FakeModel`` returns immediately,
so wall-clock time here measures orchestration overhead and nothing else.
The reported figure is ``model_calls``: sequential round trips that cannot
be overlapped. Multiply by your provider's per-call time for an estimate.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

from northstar_contracts import (
    Message,
    Money,
    RunState,
    ToolCall,
    ToolSpec,
    World,
)
from northstar_runtime import (
    AgentLoop,
    FakeModel,
    ModelProvider,
    ModelResponse,
    ToolRegistry,
)

__all__ = [
    "AMOUNT_CENTS",
    "MESSAGE_BODY",
    "ORDER_ID",
    "REASON",
    "SKU",
    "TASK",
    "Meter",
    "Metered",
    "Pattern",
    "build_loop",
    "fresh_world",
    "script",
    "tools_for",
]

ORDER_ID = "NR-2026-0041827"
SKU = "NR-LAMPSHADE-03"
AMOUNT_CENTS: Money = 3250
REASON = "damaged"
TASK = (
    "Ticket 8812: the lamp shade in order NR-2026-0041827 arrived cracked. "
    "Refund it and tell the customer."
)
MESSAGE_BODY = (
    "We have refunded US$32.50 for the cracked lamp shade. Sorry about that."
)


# --------------------------------------------------------------- the meter


@dataclass
class Meter:
    """What one pattern spent, summed over every model call it made.

    A pattern is not one loop. Routing puts a classification call in front
    of the loop, planning puts a generation call in front of it, critique
    puts a review call behind it, and search puts six in front. The meter is
    the only place all of them are visible.
    """

    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0

    @property
    def tokens(self) -> int:
        """Every token sent to or returned by a model, this pattern."""
        return self.input_tokens + self.output_tokens

    def record(self, response: ModelResponse) -> None:
        """Charge one model call to this pattern."""
        self.calls += 1
        self.input_tokens += response.input_tokens
        self.output_tokens += response.output_tokens


class Metered:
    """A provider that charges every call to a shared :class:`Meter`."""

    def __init__(self, base: ModelProvider, meter: Meter) -> None:
        self.base = base
        self.meter = meter

    def complete(
        self,
        messages: list[Message],
        tools: list[ToolSpec],
    ) -> ModelResponse:
        """Delegate, and record what the turn cost."""
        response = self.base.complete(messages, tools)
        self.meter.record(response)
        return response


@dataclass
class Pattern:
    """One build of the task: something to run, and its meter.

    Args:
        name: Row label in the cost table.
        meter: Charged by every provider this build uses.
        runner: Takes the goal, returns the final state.
        caught: Set by the build if the pattern itself detected that the
            world disagreed with the run's account of itself. Only one of
            the six can set it, and that is the chapter's point.
    """

    name: str
    meter: Meter
    runner: Callable[[str], RunState]
    caught: bool = False
    notes: list[str] = field(default_factory=list)

    def run(self, goal: str) -> RunState:
        """Execute the build and hand back the final state."""
        return self.runner(goal)

    @property
    def model_calls(self) -> int:
        """Sequential model round trips, the whole pattern."""
        return self.meter.calls

    @property
    def tokens(self) -> int:
        """Estimated tokens, the whole pattern."""
        return self.meter.tokens


# ------------------------------------------------------------- the fixture


def fresh_world() -> World:
    """A world nobody has written to yet."""
    return World()


def tools_for(world: World, names: Sequence[str] | None = None) -> ToolRegistry:
    """Register the Northstar tools, or the subset a branch was given.

    ``inject_idempotency_key`` is left off, which is Chapter 1's defect
    still in place. It has to be: this chapter is about whether a reasoning
    pattern notices the consequences, and a harness that quietly fixes the
    bug would answer the question for us.
    """
    registry = ToolRegistry()
    for spec, fn in world.tools():
        if names is None or spec.name in names:
            registry.register(spec, fn)
    return registry


def _observations(messages: Sequence[Message]) -> list[dict[str, Any]]:
    """Every tool observation the run has seen so far."""
    return [
        m.content
        for m in messages
        if m.role == "tool" and isinstance(m.content, dict)
    ]


def _succeeded(observations: Sequence[dict[str, Any]], tool: str) -> bool:
    """Whether ``tool`` has already returned a result the model believed."""
    return any(o.get("tool") == tool and o.get("ok") for o in observations)


def _attempts(observations: Sequence[dict[str, Any]], tool: str) -> int:
    """How many times ``tool`` has been called, successfully or not."""
    return sum(1 for o in observations if o.get("tool") == tool)


def _decide(messages: list[Message]) -> ToolCall | str:
    """Choose the next turn from what the run has observed.

    Written as a function of the observations rather than as a fixed list,
    so the same script serves the clean fixture and the injected-timeout
    fixture. On the timeout the model does exactly what Northstar's did:
    it sees a failure, concludes the refund did not land, and calls again.
    """
    seen = _observations(messages)
    if not _succeeded(seen, "get_order"):
        return ToolCall("c1", "get_order", {"order_id": ORDER_ID})
    if not _succeeded(seen, "get_policy"):
        return ToolCall("c2", "get_policy", {"reason": REASON, "sku": SKU})
    if not _succeeded(seen, "issue_refund"):
        attempt = _attempts(seen, "issue_refund") + 1
        return ToolCall(
            f"c3-{attempt}",
            "issue_refund",
            {
                "order_id": ORDER_ID,
                "amount_cents": AMOUNT_CENTS,
                "reason": REASON,
            },
        )
    if not _succeeded(seen, "send_message"):
        return ToolCall(
            "c4",
            "send_message",
            {"order_id": ORDER_ID, "body": MESSAGE_BODY},
        )
    return (
        "I have refunded US$32.50 to the original payment method for the "
        "cracked lamp shade and emailed the customer to confirm."
    )


#: How many turns the script is allowed to decide. Eight is generous: the
#: clean path takes five and the timeout path six.
MAX_TURNS = 8


def script() -> list[Any]:
    """The trajectory, expressed as one decision function per turn."""
    return [_decide] * MAX_TURNS


def build_loop(
    world: World,
    meter: Meter,
    *,
    tool_names: Sequence[str] | None = None,
    system_prompt: str | None = None,
    max_turns: int = 12,
) -> AgentLoop:
    """The loop every pattern edits, wired to the shared meter."""
    return AgentLoop(
        Metered(FakeModel(default=script()), meter),
        tools_for(world, tool_names),
        max_turns=max_turns,
        budget_cents=400,
        system_prompt=system_prompt,
    )
