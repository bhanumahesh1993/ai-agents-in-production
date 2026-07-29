"""Cost attribution, per run and per model.

Token counts are not the number anyone actually needs. The number that
matters is **cost per successful outcome**: an agent that resolves a refund
for eleven cents beats one that resolves nothing for two. This ledger
supplies the numerator; the evaluation harness in ``northstar_evals``
supplies the denominator.

Arithmetic is integer throughout, in nanocents, and rounds up only at the
edge. Accumulating fractional cents in a float and rounding each call is
how a monthly bill and a dashboard end up disagreeing by four percent.

**On the prices below.** They are placeholders, not price claims. Provider
pricing changes, differs by region, tier, and cache state, and is not
something a book can pin down for you. Supply your own table, dated, and
re-check it on the schedule in ``VERSIONS.md``.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from northstar_contracts import Money

__all__ = [
    "ILLUSTRATIVE_PRICES",
    "CostEntry",
    "CostLedger",
    "ModelPrice",
]

#: Nanocents per cent. Everything accumulates in nanocents.
NANOCENTS = 1_000_000_000


@dataclass(frozen=True)
class ModelPrice:
    """What one model costs, in cents per million tokens.

    Args:
        input_cents_per_million: Cost of a million prompt tokens.
        output_cents_per_million: Cost of a million completion tokens.
        note: Where the numbers came from and when. Fill this in. A price
            table with no provenance is a price table nobody dares change.
    """

    input_cents_per_million: int
    output_cents_per_million: int
    note: str = ""

    def nanocents_for(self, input_tokens: int, output_tokens: int) -> int:
        """Exact cost of one call, in nanocents."""
        per_token = NANOCENTS // 1_000_000
        return (
            input_tokens * self.input_cents_per_million * per_token
            + output_tokens * self.output_cents_per_million * per_token
        )


#: Placeholder prices. The mock models cost nothing, which is true. The
#: fallback is a round number chosen to make the examples legible, and is
#: not a claim about any real provider.
ILLUSTRATIVE_PRICES: dict[str, ModelPrice] = {
    "fake-model-1": ModelPrice(0, 0, note="mock mode is free"),
    "flaky-model-1": ModelPrice(0, 0, note="mock mode is free"),
}

#: Used for any model not in the table. Replace it with your own.
DEFAULT_PRICE = ModelPrice(
    input_cents_per_million=300,
    output_cents_per_million=1500,
    note="ILLUSTRATIVE PLACEHOLDER - replace with your dated price table",
)


@dataclass(frozen=True)
class CostEntry:
    """One model call, priced."""

    model: str
    input_tokens: int
    output_tokens: int
    nanocents: int
    run_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """JSON-serialisable form."""
        return {
            "model": self.model,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "nanocents": self.nanocents,
            "run_id": self.run_id,
        }


def _to_cents(nanocents: int) -> Money:
    """Round nanocents up to whole cents. Never round a bill down."""
    return -(-nanocents // NANOCENTS)


class CostLedger:
    """Records what each model call cost, and attributes it to a run.

    Args:
        prices: Model name to :class:`ModelPrice`. Defaults to
            :data:`ILLUSTRATIVE_PRICES`.
        default_price: Applied to models missing from the table.
        strict: Raise on an unknown model instead of using the default.
            Turn this on in production: silently pricing an unrecognised
            model at a guessed rate is how a cost dashboard becomes
            fiction.

    Example:
        >>> ledger = CostLedger()
        >>> ledger.record("fake-model-1", 1000, 200, run_id="run-1")
        0
        >>> ledger.per_run_cents("run-1")
        0
    """

    def __init__(
        self,
        prices: Mapping[str, ModelPrice] | None = None,
        default_price: ModelPrice = DEFAULT_PRICE,
        *,
        strict: bool = False,
    ) -> None:
        self.prices: dict[str, ModelPrice] = dict(
            prices if prices is not None else ILLUSTRATIVE_PRICES
        )
        self.default_price = default_price
        self.strict = strict
        self.entries: list[CostEntry] = []

    def price_for(self, model: str) -> ModelPrice:
        """The price entry for a model.

        Raises:
            KeyError: If ``strict`` and the model is unknown.
        """
        price = self.prices.get(model)
        if price is not None:
            return price
        if self.strict:
            raise KeyError(
                f"no price for model {model!r}; add it to the ledger's "
                f"price table or construct the ledger with strict=False"
            )
        return self.default_price

    def register(self, model: str, price: ModelPrice) -> None:
        """Add or replace one model's price."""
        self.prices[model] = price

    def record(
        self,
        model: str,
        input_tokens: int,
        output_tokens: int,
        run_id: str | None = None,
    ) -> Money:
        """Price one model call and add it to the ledger.

        Returns:
            The cost of this call in whole cents, rounded up. The
            per-run and total figures are computed from exact nanocents,
            so they never drift from the sum of these.
        """
        price = self.price_for(model)
        nanocents = price.nanocents_for(input_tokens, output_tokens)
        self.entries.append(
            CostEntry(model, input_tokens, output_tokens, nanocents, run_id)
        )
        return _to_cents(nanocents)

    def per_run_cents(self, run_id: str | None = None) -> Money:
        """Total cents for one run, or for everything when ``run_id`` is None."""
        total = sum(
            e.nanocents
            for e in self.entries
            if run_id is None or e.run_id == run_id
        )
        return _to_cents(total)

    def total_cents(self) -> Money:
        """Total cents across every run."""
        return self.per_run_cents(None)

    def by_model(self) -> dict[str, Money]:
        """Cents per model, highest first."""
        totals: dict[str, int] = {}
        for entry in self.entries:
            totals[entry.model] = totals.get(entry.model, 0) + entry.nanocents
        return {
            model: _to_cents(nano)
            for model, nano in sorted(
                totals.items(), key=lambda kv: -kv[1]
            )
        }

    def by_run(self) -> dict[str, Money]:
        """Cents per run, most expensive first."""
        totals: dict[str, int] = {}
        for entry in self.entries:
            key = entry.run_id or "(unattributed)"
            totals[key] = totals.get(key, 0) + entry.nanocents
        return {
            run: _to_cents(nano)
            for run, nano in sorted(totals.items(), key=lambda kv: -kv[1])
        }

    def tokens(self, run_id: str | None = None) -> dict[str, int]:
        """Input and output token totals."""
        rows = [
            e for e in self.entries if run_id is None or e.run_id == run_id
        ]
        return {
            "input_tokens": sum(e.input_tokens for e in rows),
            "output_tokens": sum(e.output_tokens for e in rows),
            "calls": len(rows),
        }

    def report(self, run_id: str | None = None) -> dict[str, Any]:
        """A dashboard-shaped summary."""
        return {
            "run_id": run_id,
            "cents": self.per_run_cents(run_id),
            **self.tokens(run_id),
            "by_model": self.by_model(),
            "prices_are_illustrative": self.prices is ILLUSTRATIVE_PRICES
            or self.default_price is DEFAULT_PRICE,
        }

    def reset(self) -> None:
        """Drop every entry. Prices are kept."""
        self.entries.clear()
