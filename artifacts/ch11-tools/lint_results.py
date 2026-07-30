"""The CI lint over real tool results.

Separate from the conformance suite, because it checks results rather than
contracts and therefore needs the tools to actually run. It replays the
artifact's fixture calls, measures every result, and fails the build on any
tool whose real output exceeds its declared budget -- or exceeds it without
saying so, which is the worse of the two.

Run it against the unshaped ``search_orders`` and it reports thousands of
tokens against a cap of 800, untruncated, and exits non-zero. That is the
check that would have caught the opening incident's sibling failure: not the
description edit itself, which a trajectory test catches, but the version of
the same tool whose result size is a function of the data.

The reason for :class:`ResultProbe` is worth stating, because it is the one
place this file is not the obvious thing.
:meth:`northstar_runtime.registry.ToolRegistry.dispatch` truncates every
result to its cap on the way out. That is exactly right at runtime -- it is
the safety net -- and useless for a lint, because downstream of it a tool
returning 40,000 tokens and a tool returning 400 look identical. The lint has
to see what the tool produced.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import Any, Protocol

from budget import count_tokens, enforce_budget
from northstar_contracts import ToolCall, ToolResult, ToolSpec, World
from northstar_runtime import ToolRegistry
from specs import SPECS

__all__ = [
    "FIXTURES",
    "FIXTURE_ORDERS",
    "FixtureCall",
    "ResultProbe",
    "bloated_world",
    "lint",
]


@dataclass(frozen=True)
class FixtureCall:
    """One call the lint replays, with a label for the report.

    Args:
        call: The call, exactly as a model would make it.
        label: What this case is for, printed alongside the measurement.
    """

    call: ToolCall
    label: str = ""


class Dispatcher(Protocol):
    """Anything the lint can measure results from."""

    def dispatch(self, call: ToolCall) -> ToolResult:
        """Run one call and return its result."""
        ...


class ResultProbe:
    """Runs a registered tool and reports what the *tool* produced.

    Args:
        registry: Where the tools live.
        enforce: Apply :func:`budget.enforce_budget` to the raw payload, which
            is what a correctly wired library does for itself. Pass ``False``
            to measure a library where nobody wired the budget in, which is
            the case the lint exists to catch.
        specs: Contracts to enforce against. Defaults to the library's.
    """

    def __init__(
        self,
        registry: ToolRegistry,
        *,
        enforce: bool = True,
        specs: dict[str, ToolSpec] | None = None,
    ) -> None:
        self.registry = registry
        self.enforce = enforce
        self.specs = specs or SPECS

    def dispatch(self, call: ToolCall) -> ToolResult:
        """Run one call, unbudgeted unless ``enforce`` says otherwise.

        Returns:
            A :class:`~northstar_contracts.models.ToolResult`. ``truncated``
            comes from the content as well as from the result, because a tool
            that pages declares truncation in its own output and a lint that
            only reads the runtime's flag would call that untruncated.
        """
        spec = self.registry.spec_for(call.name)
        fn = self._implementation(call.name)
        if spec is None or fn is None:
            return ToolResult.failure(
                call.id,
                f"no tool named {call.name!r} in this library",
            )
        payload = fn(**call.arguments)
        if self.enforce:
            return enforce_budget(spec, {**payload, "call_id": call.id})
        flagged = bool(
            isinstance(payload, dict) and payload.get("truncated")
        )
        return ToolResult(
            call_id=call.id,
            ok=True,
            content=payload,
            truncated=flagged,
        )

    def _implementation(self, name: str) -> Any | None:
        """The bound function for one tool.

        Found by walking :meth:`ToolRegistry.bindings` rather than by keying a
        dict on the spec, because a ``ToolSpec`` holds schemas and a dict is
        not hashable.
        """
        for spec, fn in self.registry.bindings():
            if spec.name == name:
                return fn
        return None


def lint(
    cases: list[FixtureCall],
    *,
    registry: Dispatcher,
    specs: dict[str, ToolSpec] | None = None,
    out: Any = print,
) -> int:
    """Measure every fixture result against its declared budget.

    Args:
        cases: The fixture calls to replay.
        registry: A dispatcher. In CI this is a :class:`ResultProbe` over the
            real library.
        specs: Contracts. Defaults to the library's.
        out: Where the report goes. Injected so a test can capture it.

    Returns:
        The number of failures. Non-zero fails the build.
    """
    table = specs or SPECS
    failures = 0
    for c in cases:
        r: ToolResult = registry.dispatch(c.call)
        n = count_tokens(r.content)
        cap = table[c.call.name].max_result_tokens
        if n > cap:
            out(f"{c.call.name}: {n} tokens > cap {cap}")
            failures += 1
        if n > cap and not r.truncated:
            out(f"{c.call.name}: over cap and not flagged")
            failures += 1
    return failures


#: Rows the CI fixture searches over. Chosen so the unshaped search lands on
#: the chapter's figure: 6,400-odd tokens against a cap of 800. Nothing about
#: the tool changes between staging and production; the row count does, and
#: that is the entire failure mode an unbounded result has.
FIXTURE_ORDERS = 78


def bloated_world(orders: int = FIXTURE_ORDERS) -> World:
    """A world with enough orders to make an unshaped search hurt.

    The three book fixtures cloned under new ids, all under one customer, so
    a single realistic search matches all of them.
    """
    world = World()
    templates = list(world.orders.values())
    for i in range(orders):
        template = copy.deepcopy(templates[i % len(templates)])
        template["order_id"] = f"NR-2026-01{i:05d}"
        template["customer_id"] = "CUST-8841"
        world.orders[template["order_id"]] = template
    return world


#: The calls CI replays. One per tool, plus the two that matter: a search
#: against a realistic row count, and a receipt, which is the smallest result
#: in the library and has the tightest cap.
FIXTURES: list[FixtureCall] = [
    FixtureCall(
        ToolCall("f1", "get_order", {"order_id": "NR-2026-0041827"}),
        "the full record, item lines included",
    ),
    FixtureCall(
        ToolCall("f2", "get_policy", {"reason": "damaged"}),
        "one policy rule",
    ),
    FixtureCall(
        ToolCall(
            "f3",
            "search_orders",
            {"customer_id": "CUST-8841", "page_size": 5},
        ),
        "a realistic page of search results",
    ),
    FixtureCall(
        ToolCall(
            "f4",
            "preview_refund",
            {
                "order_id": "NR-2026-0041827",
                "amount_cents": 3250,
                "reason": "damaged",
            },
        ),
        "the lamp shade, previewed",
    ),
    FixtureCall(
        ToolCall(
            "f5",
            "issue_refund",
            {
                "order_id": "NR-2026-0041827",
                "amount_cents": 3250,
                "reason": "damaged",
                "idempotency_key": "fixture-key-0000000001",
            },
        ),
        "a receipt, against the tightest cap in the library",
    ),
    FixtureCall(
        ToolCall(
            "f6",
            "run_code",
            {
                "program": "print(sum(inputs['cents']))",
                "inputs": {"cents": [5150, 3250]},
            },
        ),
        "a two-line aggregation",
    ),
]


@dataclass
class LintReport:
    """A captured lint run, for a test that wants the lines as well as the count.

    Attributes:
        lines: What the lint printed, in order.
        failures: How many checks failed.
    """

    lines: list[str] = field(default_factory=list)
    failures: int = 0

    def record(self, line: str) -> None:
        """Collect one reported line."""
        self.lines.append(str(line))
