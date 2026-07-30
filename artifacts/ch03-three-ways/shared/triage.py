"""The triage agent, defined once, outside every framework.

Everything a framework would otherwise make you rewrite lives here: the tool
contract as data, the world binding, the scripted trajectory, and the
idempotency key. Written as a decorator on a function, the refund contract
would have to be declared three times, once per runtime, which is the
tool-schema lock-in Chapter 3 measures.

The scenario is fixed. Order ``NR-2026-0041903`` is a single travel mug at
3,250 cents, flagged ``damaged_on_arrival``, which is below the 5,000-cent
approval threshold and therefore inside the agent's autonomy budget.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from northstar_contracts import (
    Money,
    ToolCall,
    ToolSpec,
    World,
    idempotency_key,
)
from northstar_runtime import FakeModel, SimulatedCrash, ToolRegistry

__all__ = [
    "AMOUNT_CENTS",
    "DB_PATH",
    "EXPECTED_CALLS",
    "GOAL",
    "ORDER_ID",
    "REASON",
    "REFUND",
    "REFUND_STEP",
    "RUN_ID",
    "SKU",
    "SPECS",
    "fresh_world",
    "forget_checkpoints",
    "model_for",
    "refund_amounts",
    "refund_key",
    "registry",
    "script",
]

#: The ticket every port is handed.
ORDER_ID = "NR-2026-0041903"
SKU = "NR-MUG-02"
AMOUNT_CENTS: Money = 3250
REASON = "damaged"
GOAL = (
    "Ticket 8907: the travel mug in order NR-2026-0041903 arrived cracked."
)

#: A fixed run id, so the derived key is the same on every machine and the
#: replay in ``test_equivalence.py`` presents the same identity twice.
RUN_ID = "run_01H3WAY"

#: The turn at which the refund is issued: read the order, read the policy,
#: then move money. The key is derived from this, never generated.
REFUND_STEP = 2

#: The trajectory all three runtimes must produce.
EXPECTED_CALLS = ("get_order", "get_policy", "issue_refund")

#: The checkpoint file the two durable ports share. ``ports/raw.py`` spells
#: the same string out inline, because the book prints that line.
DB_PATH = "ch03.db"


def forget_checkpoints() -> None:
    """Delete the checkpoint file, so a measurement starts from nothing."""
    Path(DB_PATH).unlink(missing_ok=True)


REFUND = ToolSpec(
    name="issue_refund",
    description="Refund an order once, keyed by idempotency_key.",
    input_schema={
        "type": "object",
        "properties": {
            "order_id": {"type": "string"},
            "amount_cents": {"type": "integer"},
            "idempotency_key": {"type": "string"},
        },
        "required": ["order_id", "amount_cents",
                     "idempotency_key"],
    },
    output_schema={"type": "object"},
    writes=True,
    idempotent=True,
    max_result_tokens=400,
)
# Executes under the support-agent principal, refunds.write.

#: ``idempotency_key`` is *required*, not optional. A write tool whose key
#: is optional is a write tool that will eventually be called without one.


def refund_key(run_id: str = RUN_ID, step: int = REFUND_STEP) -> str:
    """Derive the refund's idempotency key from the run and the step.

    Pure function of ``(run_id, step)``, so the first attempt, a retry after
    a timeout, a replay of the whole run, and a second worker resuming from
    a checkpoint all present the same identity to the refund service.
    """
    return idempotency_key(run_id, step)


def fresh_world() -> World:
    """A world nobody has written to yet."""
    return World()


def _bind_refund(world: World) -> Callable[..., Any]:
    """Bind :meth:`World.issue_refund` to the triage definition's reason.

    The reason is a property of *this* triage flow, not a decision the model
    gets to make, so it is closed over here rather than exposed in the
    schema the model reads.
    """

    def issue_refund(
        order_id: str,
        amount_cents: Money,
        idempotency_key: str,
    ) -> dict[str, Any]:
        return world.issue_refund(
            order_id,
            amount_cents,
            reason=REASON,
            idempotency_key=idempotency_key,
        )

    return issue_refund


def _read_bindings(world: World) -> list[tuple[ToolSpec, Callable[..., Any]]]:
    """The two read tools, taken straight from the world's own bindings."""
    wanted = {"get_order", "get_policy"}
    return [(spec, fn) for spec, fn in world.tools() if spec.name in wanted]


SPECS: list[ToolSpec] = [
    *[spec for spec, _ in World().tools() if spec.name in
      {"get_order", "get_policy"}],
    REFUND,
]


class CrashingRegistry(ToolRegistry):
    """A registry that dies once, after a named tool's effect has landed.

    This is the ``SIGKILL`` in criterion three, expressed in a way three
    different runtimes can all be subjected to identically. The crash lands
    *after* the write and *before* the runtime records anything about it,
    which is the only interesting moment to die at.
    """

    def __init__(self, crash_after: str, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.crash_after = crash_after
        self.fired = False

    def dispatch(
        self,
        call: ToolCall,
        run_id: str | None = None,
        step: int | None = None,
    ) -> Any:
        result = super().dispatch(call, run_id, step)
        if call.name == self.crash_after and not self.fired:
            self.fired = True
            raise SimulatedCrash(
                f"worker killed after {call.name} committed, before the "
                f"runtime recorded it"
            )
        return result


def registry(world: World, crash_after: str | None = None) -> ToolRegistry:
    """Wire the three tools onto ``world``.

    Args:
        world: The authoritative store the agent acts on.
        crash_after: Kill the worker once, immediately after this tool's
            effect has landed.
    """
    reg: ToolRegistry = (
        CrashingRegistry(crash_after) if crash_after else ToolRegistry()
    )
    for spec, fn in _read_bindings(world):
        reg.register(spec, fn)
    reg.register(REFUND, _bind_refund(world))
    return reg


def script(run_id: str = RUN_ID) -> list[Any]:
    """The scripted trajectory: read, check policy, refund, reply.

    Fixed so that any difference between the three ports is the runtime's
    and not the model's. The refund carries the derived key as an argument,
    because the key belongs to the intent rather than to the attempt.
    """
    return [
        ToolCall("c1", "get_order", {"order_id": ORDER_ID}),
        ToolCall("c2", "get_policy", {"sku": SKU, "reason": REASON}),
        ToolCall(
            "c3",
            "issue_refund",
            {
                "order_id": ORDER_ID,
                "amount_cents": AMOUNT_CENTS,
                "idempotency_key": refund_key(run_id),
            },
        ),
        "Refunded US$32.50 for the cracked travel mug.",
    ]


def model_for(run_id: str = RUN_ID) -> FakeModel:
    """The deterministic provider all three ports share."""
    return FakeModel(default=script(run_id))


def refund_amounts(world: World, order_id: str = ORDER_ID) -> list[Money]:
    """Every refund row against one order, oldest first, in cents.

    The equivalence test asserts on this rather than on run status. Two
    rows summing to the right total and one row of the right size are
    different worlds, and only the row list tells them apart.
    """
    return [r.amount_cents for r in world.refunds_for(order_id)]
