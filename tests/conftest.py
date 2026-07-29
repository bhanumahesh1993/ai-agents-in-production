"""Shared fixtures. Everything here is offline and deterministic."""

from __future__ import annotations

from collections.abc import Callable, Iterator

import pytest
from northstar_contracts import ToolCall, World
from northstar_runtime import FakeModel, ToolRegistry

#: Order fixtures the tests refer to by name.
DELIVERED_ORDER = "NR-2026-0041827"  # 8400c, 2 items
DAMAGED_ORDER = "NR-2026-0041903"  # 3250c, damaged
FLAGGED_ORDER = "NR-2026-0042110"  # 24000c, fraud review


@pytest.fixture
def clock() -> Callable[[], float]:
    """A monotonic fake clock, so timestamps are reproducible."""
    ticks = iter(range(1, 100_000))

    def tick() -> float:
        return float(next(ticks))

    return tick


@pytest.fixture
def world(clock: Callable[[], float]) -> World:
    """A fresh Northstar world with deterministic timestamps."""
    return World(clock=clock)


@pytest.fixture
def registry(world: World) -> ToolRegistry:
    """A registry over the world, with no idempotency stamping."""
    return ToolRegistry().register_all(world.tools())


@pytest.fixture
def safe_registry(world: World) -> ToolRegistry:
    """A registry that stamps idempotency keys on keyed write tools."""
    return ToolRegistry(inject_idempotency_key=True).register_all(
        world.tools()
    )


def refund_script(
    order_id: str = DAMAGED_ORDER,
    cents: int = 3250,
    reason: str = "damaged",
) -> list[object]:
    """The book's happy-path script: read, check policy, refund, explain."""
    return [
        ToolCall("c1", "get_order", {"order_id": order_id}),
        ToolCall("c2", "get_policy", {"reason": reason}),
        ToolCall(
            "c3",
            "issue_refund",
            {
                "order_id": order_id,
                "amount_cents": cents,
                "reason": reason,
            },
        ),
        f"I have refunded US${cents / 100:.2f} for the {reason} item.",
    ]


@pytest.fixture
def model() -> Iterator[FakeModel]:
    """A model scripted for the damaged-mug refund."""
    yield FakeModel(default=refund_script())
