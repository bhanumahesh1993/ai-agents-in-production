"""Wiring the eight contracts to eight implementations.

The tools shape and fit their own results here, before the runtime's
truncation ever sees them, because shaping is the tool author's job: they are
the only person who knows which fields matter. ``get_order`` on the world
returns a customer id, a placed-at date, a currency, and a flag list, none of
which a refund decision needs; ``ORDER_OUTPUT`` declares six fields and
:func:`budget.shape` drops the rest.

:func:`budgeted` wraps every tool with :func:`budget.enforce_budget` and uses
:func:`functools.wraps`, which matters for more than tidiness: the conformance
suite inspects the implementation's signature against the input schema, and
``functools.wraps`` sets ``__wrapped__`` so ``inspect.signature`` still sees
the real parameters rather than ``**arguments``. A wrapper that hid the
signature would silently disable that check.
"""

from __future__ import annotations

import functools
from collections.abc import Callable
from typing import Any

from budget import enforce_budget
from conformance import ConformingRegistry
from northstar_contracts import ToolSpec, World
from refund import RefundPath, SideEffectLedger
from sandbox import NullSandbox, SandboxContract
from specs import (
    ESCALATE_TO_SPECIALIST,
    GET_ORDER,
    GET_POLICY,
    ISSUE_REFUND,
    PREVIEW_REFUND,
    RUN_CODE,
    SEARCH_ORDERS,
    SEND_MESSAGE,
)

__all__ = [
    "Library",
    "budgeted",
    "build_library",
    "search_orders_of",
    "unshaped_search_orders_of",
]


def budgeted(
    spec: ToolSpec,
    fn: Callable[..., dict[str, Any]],
) -> Callable[..., dict[str, Any]]:
    """Shape and fit a tool's own output before anything downstream sees it.

    Args:
        spec: The contract. Drives both the shaping and the cap.
        fn: The raw implementation.

    Returns:
        A wrapper returning content only, with ``truncated`` set in the
        content when the result was cut, because a truncated result that does
        not say so is a correctness bug: the agent reasons about a partial
        list as though it were complete.
    """

    @functools.wraps(fn)
    def wrapped(*args: Any, **kwargs: Any) -> dict[str, Any]:
        payload = fn(*args, **kwargs)
        result = enforce_budget(spec, {**payload, "call_id": "-"})
        content = dict(result.content)
        if result.truncated:
            content["truncated"] = True
        return content

    return wrapped


def search_orders_of(world: World) -> Callable[..., dict[str, Any]]:
    """Progressive disclosure, done properly: ids plus one line of context.

    The rows carry the order id, the status, the total in cents, an item
    count, and the flags. They do *not* carry the item-level breakdown, and
    the description says so and says what to call next, which is the pair of
    sentences the opening incident's four-line diff deleted.

    The trade is honest and it is not always the right one. Progressive
    disclosure is right when the candidate set is large and the choice is
    cheap to make from a summary. It is wrong when the agent needs three
    fields from every candidate, in which case return those three fields for
    all of them and skip the second call.
    """

    def search_orders(
        customer_id: str | None = None,
        status: str | None = None,
        flag: str | None = None,
        page: int = 1,
        page_size: int = 5,
    ) -> dict[str, Any]:
        """Search orders, returning summaries the caller can choose from."""
        matches = [
            order
            for order in world.orders.values()
            if (customer_id is None or order["customer_id"] == customer_id)
            and (status is None or order["status"] == status)
            and (flag is None or flag in order["flags"])
        ]
        matches.sort(key=lambda o: o["order_id"])
        start = (page - 1) * page_size
        window = matches[start : start + page_size]
        returned = start + len(window)
        return {
            "results": [
                {
                    "order_id": order["order_id"],
                    "status": order["status"],
                    "total_cents": order["total_cents"],
                    "item_count": sum(i["quantity"] for i in order["items"]),
                    "flags": list(order["flags"]),
                }
                for order in window
            ],
            "total_matches": len(matches),
            "next_page": page + 1 if returned < len(matches) else None,
            "truncated": False,
        }

    return search_orders


def unshaped_search_orders_of(world: World) -> Callable[..., dict[str, Any]]:
    """The same search, returning everything the store holds.

    Kept as a fixture so the lint has something to fail on. Its size is a
    function of the data rather than of the contract, which is why it worked
    in staging against three orders and blows the context budget in
    production against forty.
    """

    def search_orders(
        customer_id: str | None = None,
        status: str | None = None,
        flag: str | None = None,
        page: int = 1,
        page_size: int = 5,
    ) -> dict[str, Any]:
        """Search orders, returning whole records because the API did."""
        matches = [
            dict(order)
            for order in world.orders.values()
            if (customer_id is None or order["customer_id"] == customer_id)
            and (status is None or order["status"] == status)
            and (flag is None or flag in order["flags"])
        ]
        matches.sort(key=lambda o: o["order_id"])
        return {
            "results": matches,
            "total_matches": len(matches),
            "next_page": None,
        }

    return search_orders


class Library:
    """The registered library, with everything it needed to build itself.

    Attributes:
        registry: A :class:`conformance.ConformingRegistry`, so nothing that
            failed :func:`conformance.check` is in it.
        world: The system of record the tools act on.
        ledger: The side-effect ledger the writes record to.
        path: The bound refund path.
        sandbox: The code-execution sandbox.
    """

    def __init__(
        self,
        world: World,
        ledger: SideEffectLedger,
        sandbox: NullSandbox,
        *,
        registry: ConformingRegistry,
        path: RefundPath,
    ) -> None:
        self.world = world
        self.ledger = ledger
        self.sandbox = sandbox
        self.registry = registry
        self.path = path

    def specs(self) -> list[ToolSpec]:
        """Every registered spec, in registration order."""
        return self.registry.specs()

    def read_only_names(self) -> list[str]:
        """The tools a read-only agent may hold.

        Not a promise in a document: a property of the registered set that a
        policy engine can enforce and an auditor can check.
        """
        return [s.name for s in self.specs() if not s.writes]

    def write_names(self) -> list[str]:
        """The tools that change the world."""
        return [s.name for s in self.specs() if s.writes]


def build_library(
    world: World | None = None,
    ledger: SideEffectLedger | None = None,
    sandbox: NullSandbox | None = None,
    *,
    search_description: str | None = None,
    unshaped_search: bool = False,
    inject_idempotency_key: bool = True,
) -> Library:
    """Register the eight tools, refusing any that fails conformance.

    Args:
        world: The system of record. A fresh one by default.
        ledger: The side-effect ledger. A fresh one by default.
        sandbox: The code sandbox. A fresh :class:`sandbox.NullSandbox` with
            the default contract by default.
        search_description: Override ``search_orders``' description. Pass
            :data:`specs.SEARCH_ORDERS_DRIFTED` to build the library as it
            was after the four-line diff.
        unshaped_search: Register the search that returns whole records, for
            the lint to fail on.
        inject_idempotency_key: Let the registry stamp write calls from
            ``(run_id, step, call_id)``. On by default here, unlike the
            runtime's default, because every write in this library declares
            the key as required and a model should not be inventing one.

    Returns:
        The :class:`Library`.

    Raises:
        conformance.ConformanceError: If any tool fails a rule.
    """
    world = world or World()
    ledger = ledger or SideEffectLedger()
    sandbox = sandbox or NullSandbox(SandboxContract())
    path = RefundPath(world=world, ledger=ledger)
    registry = ConformingRegistry(
        inject_idempotency_key=inject_idempotency_key
    )

    search_spec = SEARCH_ORDERS
    if search_description is not None:
        search_spec = ToolSpec(
            name=SEARCH_ORDERS.name,
            description=search_description,
            input_schema=SEARCH_ORDERS.input_schema,
            output_schema=SEARCH_ORDERS.output_schema,
            writes=SEARCH_ORDERS.writes,
            idempotent=SEARCH_ORDERS.idempotent,
            max_result_tokens=SEARCH_ORDERS.max_result_tokens,
            version="5",
        )
    search_fn = (
        unshaped_search_orders_of(world)
        if unshaped_search
        else search_orders_of(world)
    )

    def run_code(
        program: str,
        inputs: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Run a short program in the sandbox, with the caller's inputs."""
        return sandbox.run(program, inputs)

    pairs: list[tuple[ToolSpec, Callable[..., dict[str, Any]]]] = [
        (GET_ORDER, world.get_order),
        (GET_POLICY, world.get_policy),
        (search_spec, search_fn),
        (PREVIEW_REFUND, path.preview_refund),
        (ISSUE_REFUND, path.issue_refund),
        (SEND_MESSAGE, path.send_message),
        (ESCALATE_TO_SPECIALIST, path.escalate_to_specialist),
        (RUN_CODE, run_code),
    ]
    for spec, fn in pairs:
        # The unshaped search is registered *raw*, with no budget wrapper.
        # That is the library the lint exists to catch: the contract declares
        # a cap of 800 and nothing enforces it, so the result size is a
        # function of the row count rather than of the contract.
        unwired = unshaped_search and spec.name == "search_orders"
        registry.register(spec, fn if unwired else budgeted(spec, fn))

    return Library(
        world, ledger, sandbox, registry=registry, path=path
    )
