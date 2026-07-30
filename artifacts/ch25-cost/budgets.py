"""Per-run budgets that fail closed, and the prices used to compute them.

Every price in this module is a **placeholder**. They are round numbers
chosen so the arithmetic in the chapter is legible, not claims about any
provider's rates. Replace them with your own dated table before you draw a
conclusion about real money; ``VERSIONS.md`` says when to re-check.

The budget itself is not a placeholder. ``BudgetGuard`` caps cents, turns,
and wall clock, every limit raises, and the loop does not let the model
vote on any of them.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable

from northstar_contracts import Money, ToolSpec
from northstar_runtime import AgentLoop, ModelProvider, ModelResponse, ToolRegistry
from northstar_telemetry import ModelPrice
from northstar_telemetry.cost import NANOCENTS

__all__ = [
    "BUDGET_CENTS",
    "CACHE_READ_MULTIPLIER",
    "HUMAN_HANDLING_CENTS",
    "HUMAN_HANDLING_SECONDS",
    "ILLUSTRATIVE_PRICES",
    "LARGE_MODEL",
    "MAX_TURNS",
    "MAX_WALL_SECONDS",
    "SMALL_MODEL",
    "budgeted_loop",
    "priced",
    "to_cents",
]

#: The two model classes the router chooses between. Names, not products.
LARGE_MODEL = "large-model-1"
SMALL_MODEL = "small-model-1"

_NOTE = "ILLUSTRATIVE PLACEHOLDER - replace with your dated price table"

#: Cents per million tokens. Illustrative. The only property the chapter's
#: arithmetic needs is that the small model is an order of magnitude
#: cheaper than the large one and that output costs more than input.
ILLUSTRATIVE_PRICES: dict[str, ModelPrice] = {
    LARGE_MODEL: ModelPrice(300, 1500, note=_NOTE),
    SMALL_MODEL: ModelPrice(30, 150, note=_NOTE),
}

#: What a cache-read input token costs, as a fraction of an uncached one.
#: Providers publish a discount of this rough shape; the exact figure is
#: theirs to state and yours to re-check.
CACHE_READ_MULTIPLIER = 0.1

#: What a failed run costs once a person picks it up. The chapter's
#: arithmetic: 180 seconds of handling at an illustrative US$6/hour.
HUMAN_HANDLING_SECONDS = 180
HUMAN_CENTS_PER_HOUR = 600
HUMAN_HANDLING_CENTS: Money = (
    HUMAN_CENTS_PER_HOUR * HUMAN_HANDLING_SECONDS // 3600
)

#: The three limits the chapter's excerpt sets on every run.
BUDGET_CENTS: Money = 120
MAX_TURNS = 12
MAX_WALL_SECONDS = 90.0


def to_cents(nanocents: int) -> Money:
    """Round nanocents up to whole cents. Never round a bill down."""
    return -(-nanocents // NANOCENTS)


def priced(
    input_per_m: int,
    output_per_m: int,
) -> Callable[[ModelResponse], Money]:
    """Build the loop's ``cost_fn`` from a price in cents per million.

    Args:
        input_per_m: Cents per million prompt tokens.
        output_per_m: Cents per million completion tokens.

    Returns:
        A callable the loop charges its budget with. Integer cents,
        rounded up, so a run cannot spend a fraction of a cent forever.
    """
    price = ModelPrice(input_per_m, output_per_m, note=_NOTE)

    def cost(response: ModelResponse) -> Money:
        return to_cents(
            price.nanocents_for(response.input_tokens, response.output_tokens)
        )

    return cost


def budgeted_loop(
    model: ModelProvider,
    tools: ToolRegistry | Iterable[tuple[ToolSpec, object]],
    **overrides: object,
) -> AgentLoop:
    """Wire a loop with the three limits set and a deterministic price.

    This is the chapter's excerpt with the defaults named rather than
    inlined, so the demo, the tests, and the book agree on one set of
    numbers::

        loop = AgentLoop(
            model=FakeModel(default=script),
            tools=registry,
            budget_cents=120,     # a hard ceiling, not a monthly review
            max_turns=12,
            max_wall_seconds=90,
            cost_fn=priced(input_per_m=300, output_per_m=1500),
        )

    Args:
        model: Any :class:`ModelProvider`.
        tools: Registry, or spec/implementation pairs.
        **overrides: Passed straight to :class:`AgentLoop`, so a caller can
            lower a limit to prove it fails closed.

    Returns:
        A configured :class:`AgentLoop`.
    """
    large = ILLUSTRATIVE_PRICES[LARGE_MODEL]
    kwargs: dict[str, object] = {
        "budget_cents": BUDGET_CENTS,
        "max_turns": MAX_TURNS,
        "max_wall_seconds": MAX_WALL_SECONDS,
        "cost_fn": priced(
            input_per_m=large.input_cents_per_million,
            output_per_m=large.output_cents_per_million,
        ),
    }
    kwargs.update(overrides)
    return AgentLoop(model=model, tools=tools, **kwargs)  # type: ignore[arg-type]
