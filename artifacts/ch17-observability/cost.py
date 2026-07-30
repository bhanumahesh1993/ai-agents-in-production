"""Cost as trace-linked events, rolled up to the run and then to a success.

Token counts on a span are not cost. Turning them into cost is a small piece
of engineering most teams defer and then rebuild under pressure, usually during
a month-end conversation with finance. Four details separate a cost view that
survives contact with an accountant from one that does not, and all four are
mechanised here.

**Cached tokens are a separate line.** Every turn of an agent loop re-sends an
accumulating history that is mostly identical to the previous turn's. The split
is computed rather than declared: the cached portion of a turn's prompt is what
the previous turn already sent.

**Retries and failed calls cost money.** Every model call is recorded,
successful or not. A ledger that only records successful calls is low by
exactly the amount your reliability problems are costing you, which is the
number you most want to see.

**Subagent cost rolls up to the parent run.** Every entry carries the *root*
run id, not just the local one. Attribution follows the span tree, so it
follows context propagation, so a broken handoff produces spend with no owner
-- and :meth:`CostLedger.unattributed_cents` is what that looks like as a
number.

**Cost per run is the wrong denominator.** The number that means something is
cost per successful, verified outcome. A change that cuts cost per run by a
fifth while cutting success by a third has made the system more expensive, and
only the second denominator shows it.

Recording is keyed on ``(run_id, span_id)``, which makes it idempotent under
replay. That matters because Chapter 24's durable runner will replay steps.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from northstar_contracts import Money
from northstar_telemetry import ModelPrice

__all__ = [
    "NORTHSTAR_PRICES",
    "PRICING_VERSION",
    "CostEvent",
    "CostLedger",
    "Price",
]

NANOCENTS = 1_000_000_000

#: The rate card these examples run against. It stops a rate-card change from
#: silently rewriting last month's numbers, which is what lets you answer
#: "what did we think this cost at the time" separately from "what would it
#: cost today".
PRICING_VERSION = "2026-07-27"


@dataclass(frozen=True)
class Price:
    """What one model costs, with the cached input rate carried separately.

    Args:
        version: The rate card this came from. Recorded on every event.
        uncached: Input and output rates, in cents per million tokens.
        cached_input_cents_per_million: What a cache hit on the prompt
            costs. Provider-specific and usually a fraction of the
            uncached rate, which is exactly why a single-rate ledger
            overstates an agent loop's bill.
        note: Where the numbers came from. Fill this in. A price table with
            no provenance is a price table nobody dares change.
    """

    version: str
    uncached: ModelPrice
    cached_input_cents_per_million: int
    note: str = ""

    def nanocents_for(
        self,
        uncached_input_tokens: int,
        cached_input_tokens: int,
        output_tokens: int,
    ) -> int:
        """Exact cost of one call, in nanocents."""
        per_token = NANOCENTS // 1_000_000
        return (
            self.uncached.nanocents_for(uncached_input_tokens, output_tokens)
            + cached_input_tokens
            * self.cached_input_cents_per_million
            * per_token
        )


#: ILLUSTRATIVE PLACEHOLDERS. Prices move, differ by region and tier, and are
#: not something a book can pin down. Supply your own table, dated, and
#: re-check it on the schedule in ``VERSIONS.md``. These are non-zero so the
#: cost tables in the demo say something; they are not a claim about any
#: provider.
NORTHSTAR_PRICES: dict[str, Price] = {
    "fake-model-1": Price(
        version=PRICING_VERSION,
        uncached=ModelPrice(300, 1500, note="illustrative placeholder"),
        cached_input_cents_per_million=30,
        note="ILLUSTRATIVE PLACEHOLDER - replace with your dated table",
    ),
    "flaky-model-1": Price(
        version=PRICING_VERSION,
        uncached=ModelPrice(300, 1500, note="illustrative placeholder"),
        cached_input_cents_per_million=30,
        note="ILLUSTRATIVE PLACEHOLDER - replace with your dated table",
    ),
}

DEFAULT_PRICE = NORTHSTAR_PRICES["fake-model-1"]


@dataclass(frozen=True)
class CostEvent:
    """One priced model call, linked to the trace that produced it.

    Attributes:
        run_id: The run the span belongs to.
        root_run_id: The run every ancestor rolls up to. Equal to
            ``run_id`` for a top-level run; a child agent's spend lands
            here only if the trace context survived the hop.
        span_id: What makes recording idempotent under replay.
        component: Who spent the money -- model, tool, browser, retrieval,
            evaluation, or human time.
        pricing_version: The rate card in force when this was recorded.
    """

    run_id: str
    root_run_id: str
    span_id: str
    model: str
    component: str
    input_tokens: int
    cached_input_tokens: int
    output_tokens: int
    nanocents: int
    pricing_version: str

    def to_dict(self) -> dict[str, Any]:
        """JSON form, for a cost artifact in CI."""
        return {
            "run_id": self.run_id,
            "root_run_id": self.root_run_id,
            "span_id": self.span_id,
            "model": self.model,
            "component": self.component,
            "input_tokens": self.input_tokens,
            "cached_input_tokens": self.cached_input_tokens,
            "output_tokens": self.output_tokens,
            "nanocents": self.nanocents,
            "pricing_version": self.pricing_version,
        }


def _to_cents(nanocents: int) -> Money:
    """Round nanocents up to whole cents. Never round a bill down."""
    return -(-nanocents // NANOCENTS)


class CostLedger:
    """Trace-linked cost events, rolled up per run and per outcome.

    Args:
        prices: Model name to :class:`Price`.
        default_price: Applied to a model missing from the table.
        strict: Raise on an unknown model instead of guessing. Turn this on
            in production: silently pricing an unrecognised model at a
            guessed rate is how a cost dashboard becomes fiction.
    """

    def __init__(
        self,
        prices: dict[str, Price] | None = None,
        default_price: Price = DEFAULT_PRICE,
        *,
        strict: bool = False,
    ) -> None:
        self.prices = dict(prices or NORTHSTAR_PRICES)
        self.default_price = default_price
        self.strict = strict
        self._events: dict[tuple[str, str], CostEvent] = {}
        #: run_id to the root it rolls up to, as handoffs declare it.
        self.roots: dict[str, str] = {}

    # ------------------------------------------------------------ recording

    def link(self, run_id: str, root_run_id: str) -> None:
        """Declare which root a run's spend belongs to.

        Called by the handoff. When context propagation is broken the child
        never gets linked, and its spend shows up under
        :meth:`unattributed_cents` instead of under the run that caused it.
        """
        self.roots[run_id] = root_run_id

    def price_for(self, model: str) -> Price:
        """The rate card entry for a model.

        Raises:
            KeyError: If ``strict`` and the model is unknown.
        """
        price = self.prices.get(model)
        if price is not None:
            return price
        if self.strict:
            raise KeyError(
                f"no price for model {model!r}; add it to the ledger's rate "
                "card or construct the ledger with strict=False"
            )
        return self.default_price

    def record(
        self,
        *,
        model: str,
        input_tokens: int,
        cached_input_tokens: int = 0,
        output_tokens: int = 0,
        pricing_version: str = PRICING_VERSION,
        run_id: str = "",
        span_id: str = "",
        component: str = "model",
    ) -> Money:
        """Price one call and add it to the ledger.

        Keyed by ``(run_id, span_id)``: recording the same span twice
        overwrites rather than adds, so a replayed step cannot double-count
        spend.

        Args:
            input_tokens: Total prompt tokens, cached portion included.
            cached_input_tokens: The part of the prompt that was a cache
                hit. Priced on its own terms.

        Returns:
            The cost of this call in whole cents, rounded up.

        Raises:
            ValueError: If the cached portion exceeds the prompt, which
                would mean the split was computed against the wrong turn.
        """
        if cached_input_tokens > input_tokens:
            raise ValueError(
                f"cached_input_tokens ({cached_input_tokens}) cannot exceed "
                f"input_tokens ({input_tokens})"
            )
        price = self.price_for(model)
        nanocents = price.nanocents_for(
            input_tokens - cached_input_tokens,
            cached_input_tokens,
            output_tokens,
        )
        event = CostEvent(
            run_id=run_id,
            root_run_id=self.roots.get(run_id, run_id),
            span_id=span_id,
            model=model,
            component=component,
            input_tokens=input_tokens,
            cached_input_tokens=cached_input_tokens,
            output_tokens=output_tokens,
            nanocents=nanocents,
            pricing_version=pricing_version,
        )
        self._events[(run_id, span_id)] = event
        return _to_cents(nanocents)

    # ------------------------------------------------------------- roll-ups

    @property
    def events(self) -> list[CostEvent]:
        """Every event, in insertion order."""
        return list(self._events.values())

    def _root_of(self, event: CostEvent) -> str:
        """The root a recorded event rolls up to, re-read at report time.

        Re-read rather than frozen at record time because a handoff may be
        instrumented after the child has already spent something, and the
        answer has to be the same either way.
        """
        return self.roots.get(event.run_id, event.root_run_id)

    def per_run_cents(self, run_id: str | None = None) -> Money:
        """Cents for one root run and everything under it."""
        return _to_cents(sum(
            e.nanocents
            for e in self._events.values()
            if run_id is None or self._root_of(e) == run_id
        ))

    def total_cents(self) -> Money:
        """Cents across everything recorded."""
        return self.per_run_cents(None)

    def unattributed_cents(self, roots: set[str]) -> Money:
        """Spend that rolls up to no run in ``roots``.

        This is Northstar's April failure as a number. The total is
        unchanged; the share of it nobody can attribute is the finding.
        """
        return _to_cents(sum(
            e.nanocents
            for e in self._events.values()
            if self._root_of(e) not in roots
        ))

    def tokens(self, run_id: str | None = None) -> dict[str, int]:
        """Prompt, cached, and completion totals."""
        rows = [
            e for e in self._events.values()
            if run_id is None or self._root_of(e) == run_id
        ]
        return {
            "input_tokens": sum(e.input_tokens for e in rows),
            "cached_input_tokens": sum(
                e.cached_input_tokens for e in rows
            ),
            "output_tokens": sum(e.output_tokens for e in rows),
            "calls": len(rows),
        }

    def cost_per_success(
        self,
        attempted: list[str],
        succeeded: list[str],
        human_minutes: float = 0.0,
        minute_cents: int = 0,
    ) -> float:
        """Total spend across attempts, divided by verified successes.

        Failed runs, abandoned runs, and runs a human had to redo all
        consumed money and produced nothing, so they belong in the
        numerator. Human time belongs there too: an agent that is cheap in
        tokens because it escalates constantly has moved cost rather than
        removed it.

        Returns:
            Cents per verified success, or ``inf`` when nothing succeeded,
            which is the honest answer rather than a division error.
        """
        spend = sum(self.per_run_cents(run_id) for run_id in attempted)
        spend += int(round(human_minutes * minute_cents))
        if not succeeded:
            return float("inf")
        return spend / len(succeeded)


def cached_split(
    input_tokens: int,
    previous_input_tokens: int,
) -> tuple[int, int]:
    """Split one turn's prompt into cached and uncached parts.

    Computed, not declared: the cached portion of this turn's prompt is
    whatever the previous turn already sent, because an agent loop re-sends
    an accumulating history. On the first turn nothing is cached.

    Returns:
        ``(uncached, cached)``.
    """
    cached = max(0, min(previous_input_tokens, input_tokens))
    return input_tokens - cached, cached
