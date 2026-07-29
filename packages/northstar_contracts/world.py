"""The Northstar Returns world: the authoritative store every artifact uses.

Northstar Returns is a fictional commerce company with a support agent. The
:class:`World` here is the system of record that agent acts on — orders,
refunds, customer messages, escalations — held in memory so every example
in the book runs offline with no keys and no cloud account.

Two design choices matter more than the fixtures.

**The world is authoritative, not the transcript.** An agent that *says* it
refunded a customer has proved nothing. Outcome graders in
``northstar_evals`` assert on this object, never on what the model claimed.

**The world can lie to you on purpose.** :meth:`World.inject_fault` makes a
tool time out, error, stall, or apply twice. The Chapter 1 incident — a
refund that times out *after* the write has landed, then double-refunds on
a blind retry — is one line of setup here, and it is the failure the rest
of the book removes.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from .errors import (
    RetryableToolError,
    ToolTimeout,
    ToolValidationError,
)
from .models import Currency, Money, ToolSpec
from .tokens import estimate_tokens

__all__ = [
    "FAULT_KINDS",
    "REFUND_APPROVAL_THRESHOLD_CENTS",
    "Fault",
    "World",
]

#: Refunds at or below this stay inside the agent's autonomy budget.
#: Above it, a human decides. The number is a policy input, not a law of
#: nature: it belongs in configuration, and it is here so the book has one
#: concrete threshold to reason about.
REFUND_APPROVAL_THRESHOLD_CENTS: Money = 5000

#: Fault kinds :meth:`World.inject_fault` understands.
FAULT_KINDS: frozenset[str] = frozenset(
    {"timeout", "error", "slow", "duplicate"}
)


@dataclass
class Fault:
    """A scheduled failure for one tool.

    Args:
        kind: One of :data:`FAULT_KINDS`.

            ``timeout``
                The write lands, then the call raises
                :class:`~northstar_contracts.errors.ToolTimeout`. The caller
                cannot tell whether the effect happened. This is the
                interesting one.
            ``error``
                The call fails *before* any write. Nothing happened.
            ``slow``
                The call succeeds after ``delay_seconds``.
            ``duplicate``
                The effect is applied twice, as an at-least-once delivery
                path would. An idempotency key still collapses it to one.
        times: How many calls the fault applies to before it expires.
        delay_seconds: Sleep for ``slow``. Kept tiny so tests stay fast.
        message: Error text surfaced to the agent.
    """

    kind: str
    times: int = 1
    delay_seconds: float = 0.01
    message: str = ""

    def __post_init__(self) -> None:
        if self.kind not in FAULT_KINDS:
            known = ", ".join(sorted(FAULT_KINDS))
            raise ValueError(
                f"unknown fault kind {self.kind!r}; expected one of {known}"
            )


@dataclass
class _Refund:
    """One refund row in the authoritative store."""

    refund_id: str
    order_id: str
    amount_cents: Money
    reason: str
    currency: str
    created_at: float
    idempotency_key: str | None


def _order(
    order_id: str,
    customer_id: str,
    status: str,
    placed_at: str,
    items: list[dict[str, Any]],
    flags: list[str] | None = None,
) -> dict[str, Any]:
    """Build one order fixture, deriving the total from the line items."""
    total = sum(i["quantity"] * i["unit_price_cents"] for i in items)
    return {
        "order_id": order_id,
        "customer_id": customer_id,
        "status": status,
        "placed_at": placed_at,
        "currency": Currency.USD.value,
        "total_cents": total,
        "refunded_cents": 0,
        "items": items,
        "flags": list(flags or []),
    }


def _fixtures() -> dict[str, dict[str, Any]]:
    """The three orders every chapter in the book refers to."""
    return {
        "NR-2026-0041827": _order(
            "NR-2026-0041827",
            customer_id="CUST-8841",
            status="delivered",
            placed_at="2026-07-11",
            items=[
                {
                    "sku": "NR-HEADPHONES-01",
                    "name": "Northstar Studio Headphones",
                    "quantity": 1,
                    "unit_price_cents": 5150,
                },
                {
                    "sku": "NR-LAMPSHADE-03",
                    "name": "Linen Drum Lamp Shade, 12in",
                    "quantity": 1,
                    "unit_price_cents": 3250,
                },
            ],
        ),
        "NR-2026-0041903": _order(
            "NR-2026-0041903",
            customer_id="CUST-8841",
            status="delivered",
            placed_at="2026-07-19",
            items=[
                {
                    "sku": "NR-MUG-02",
                    "name": "Northstar Travel Mug, 470ml",
                    "quantity": 1,
                    "unit_price_cents": 3250,
                }
            ],
            flags=["damaged_on_arrival"],
        ),
        "NR-2026-0042110": _order(
            "NR-2026-0042110",
            customer_id="CUST-9032",
            status="shipped",
            placed_at="2026-07-24",
            items=[
                {
                    "sku": "NR-SPEAKER-09",
                    "name": "Northstar Field Speaker",
                    "quantity": 2,
                    "unit_price_cents": 12000,
                }
            ],
            flags=["fraud_review"],
        ),
    }


#: Refund eligibility, keyed by reason. ``sku_overrides`` lets one product
#: category deviate without forking the whole rule set.
_POLICY_RULES: list[dict[str, Any]] = [
    {
        "reason": "damaged",
        "eligible": True,
        "refund_pct": 100,
        "window_days": 30,
        "requires_evidence": True,
        "notes": "Photo evidence requested but not blocking under 5000c.",
    },
    {
        "reason": "not_delivered",
        "eligible": True,
        "refund_pct": 100,
        "window_days": 45,
        "requires_evidence": False,
        "notes": "Carrier scan is checked by the fulfilment team, not here.",
    },
    {
        "reason": "changed_mind",
        "eligible": True,
        "refund_pct": 90,
        "window_days": 14,
        "requires_evidence": False,
        "notes": "10% restocking fee applies outside 7 days.",
    },
    {
        "reason": "fraud_suspected",
        "eligible": False,
        "refund_pct": 0,
        "window_days": 0,
        "requires_evidence": True,
        "notes": "Never auto-refund. Hand to the specialist agent.",
    },
]

_SKU_OVERRIDES: dict[str, dict[str, Any]] = {
    # Consumables cannot be returned for a change of mind.
    "NR-MUG-02": {"changed_mind": {"eligible": True, "refund_pct": 50}},
}


class World:
    """An in-memory authoritative store with six tools bolted to it.

    Args:
        clock: Injectable time source. Tests pass a counter so timestamps
            are deterministic and golden trajectories stay stable.

    Attributes:
        orders: Order id to order dict.
        ledger: Append-only record of every side effect that landed. This
            is what a state grader reads, and what an auditor would ask
            for.
        calls: Append-only record of every tool invocation, including the
            ones that failed. Duplicate detection reads this.
    """

    def __init__(self, clock: Callable[[], float] | None = None) -> None:
        self._clock: Callable[[], float] = clock or time.time
        self.orders: dict[str, dict[str, Any]] = _fixtures()
        self.refunds: list[_Refund] = []
        self.messages: list[dict[str, Any]] = []
        self.escalations: list[dict[str, Any]] = []
        self.ledger: list[dict[str, Any]] = []
        self.calls: list[dict[str, Any]] = []
        self._idempotency: dict[str, dict[str, Any]] = {}
        self._faults: dict[str, Fault] = {}
        self._counter = 0

    # ---------------------------------------------------------------- setup

    def inject_fault(
        self,
        tool: str,
        kind: str = "timeout",
        *,
        times: int = 1,
        delay_seconds: float = 0.01,
        message: str = "",
    ) -> Fault:
        """Schedule the next ``times`` calls to ``tool`` to misbehave.

        ``World().inject_fault("issue_refund", kind="timeout")`` is the
        Chapter 1 incident: the refund is written, the response never comes
        back, and a blind retry refunds the customer twice.

        Returns:
            The scheduled :class:`Fault`, so a test can assert on it.
        """
        fault = Fault(
            kind=kind,
            times=times,
            delay_seconds=delay_seconds,
            message=message or f"{tool} {kind}",
        )
        self._faults[tool] = fault
        return fault

    def clear_faults(self) -> None:
        """Remove every scheduled fault."""
        self._faults.clear()

    def _take_fault(self, tool: str) -> Fault | None:
        """Consume one scheduled fault for ``tool``, if any is pending."""
        fault = self._faults.get(tool)
        if fault is None:
            return None
        fault.times -= 1
        if fault.times <= 0:
            del self._faults[tool]
        return fault

    def _next_id(self, prefix: str) -> str:
        """Monotonic, deterministic identifier."""
        self._counter += 1
        return f"{prefix}-{self._counter:05d}"

    def _record_call(self, tool: str, arguments: dict[str, Any]) -> None:
        self.calls.append(
            {"tool": tool, "arguments": dict(arguments), "ts": self._clock()}
        )

    def _record_effect(self, kind: str, payload: dict[str, Any]) -> None:
        """Append to the side-effect ledger. Nothing is ever removed."""
        self.ledger.append(
            {"kind": kind, "ts": self._clock(), **payload}
        )

    # ------------------------------------------------------------- read API

    def total_refunded_cents(self, order_id: str) -> Money:
        """Sum of every refund that landed against ``order_id``."""
        return sum(r.amount_cents for r in self.refunds if r.order_id == order_id)

    def refunds_for(self, order_id: str) -> list[_Refund]:
        """Every refund row for one order, oldest first."""
        return [r for r in self.refunds if r.order_id == order_id]

    def effects(self, kind: str | None = None) -> list[dict[str, Any]]:
        """Ledger entries, optionally filtered by effect kind."""
        if kind is None:
            return list(self.ledger)
        return [e for e in self.ledger if e["kind"] == kind]

    def call_count(self, tool: str) -> int:
        """How many times a tool was invoked, successfully or not."""
        return sum(1 for c in self.calls if c["tool"] == tool)

    def snapshot(self) -> dict[str, Any]:
        """A JSON-serialisable picture of the world, for graders and diffs."""
        return {
            "orders": {
                oid: {
                    "status": o["status"],
                    "total_cents": o["total_cents"],
                    "refunded_cents": o["refunded_cents"],
                    "flags": list(o["flags"]),
                }
                for oid, o in self.orders.items()
            },
            "refund_count": len(self.refunds),
            "message_count": len(self.messages),
            "escalation_count": len(self.escalations),
            "ledger_entries": len(self.ledger),
        }

    # ----------------------------------------------------------- the tools

    def get_order(self, order_id: str) -> dict[str, Any]:
        """Return one order, including items, status, and totals in cents."""
        self._record_call("get_order", {"order_id": order_id})
        self._guard_read("get_order")
        order = self.orders.get(order_id)
        if order is None:
            raise ToolValidationError(
                f"no order {order_id!r}. Order ids look like NR-2026-0041827."
            )
        refunded = self.total_refunded_cents(order_id)
        return {
            **{k: v for k, v in order.items() if k != "refunded_cents"},
            "refunded_cents": refunded,
            "refundable_cents": order["total_cents"] - refunded,
        }

    def get_policy(
        self,
        reason: str | None = None,
        sku: str | None = None,
    ) -> dict[str, Any]:
        """Return refund eligibility rules, optionally narrowed.

        Args:
            reason: Filter to one refund reason, for example ``"damaged"``.
            sku: Apply any product-specific override.
        """
        self._record_call("get_policy", {"reason": reason, "sku": sku})
        self._guard_read("get_policy")
        rules = [
            dict(r)
            for r in _POLICY_RULES
            if reason is None or r["reason"] == reason
        ]
        if sku:
            for rule in rules:
                override = _SKU_OVERRIDES.get(sku, {}).get(rule["reason"])
                if override:
                    rule.update(override)
                    rule["sku_override"] = sku
        return {
            "rules": rules,
            "approval_threshold_cents": REFUND_APPROVAL_THRESHOLD_CENTS,
            "currency": Currency.USD.value,
            "policy_version": "2026-07-01",
        }

    def search_orders(
        self,
        customer_id: str | None = None,
        status: str | None = None,
        flag: str | None = None,
        page: int = 1,
        page_size: int = 2,
        max_result_tokens: int = 400,
    ) -> dict[str, Any]:
        """Search orders, paginated and capped by a token budget.

        Search is the tool most likely to blow up a context window: it is
        the one whose result size the caller does not control. So it
        returns compact summaries rather than whole orders, pages by
        default, and drops rows once the estimated token cost of the page
        exceeds ``max_result_tokens`` — reporting that it did so rather
        than silently returning less than you asked for.
        """
        self._record_call(
            "search_orders",
            {"customer_id": customer_id, "status": status, "flag": flag},
        )
        self._guard_read("search_orders")
        if page < 1 or page_size < 1:
            raise ToolValidationError("page and page_size must be >= 1")

        matches = [
            o
            for o in self.orders.values()
            if (customer_id is None or o["customer_id"] == customer_id)
            and (status is None or o["status"] == status)
            and (flag is None or flag in o["flags"])
        ]
        matches.sort(key=lambda o: o["order_id"])
        start = (page - 1) * page_size
        window = matches[start : start + page_size]

        results: list[dict[str, Any]] = []
        truncated = False
        for order in window:
            summary = {
                "order_id": order["order_id"],
                "status": order["status"],
                "total_cents": order["total_cents"],
                "item_count": sum(i["quantity"] for i in order["items"]),
                "flags": list(order["flags"]),
            }
            if estimate_tokens(results + [summary]) > max_result_tokens:
                truncated = True
                break
            results.append(summary)

        returned = start + len(results)
        return {
            "results": results,
            "page": page,
            "page_size": page_size,
            "total_matches": len(matches),
            "next_page": page + 1 if returned < len(matches) else None,
            "truncated": truncated,
        }

    def issue_refund(
        self,
        order_id: str,
        amount_cents: Money,
        reason: str,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """Move money. The call the whole book is really about.

        Pass ``idempotency_key`` and a repeat is a no-op that returns the
        original refund with ``duplicate: True``. Omit it and a repeat
        refunds the customer a second time — which is correct behaviour for
        a store that cannot tell two identical intents apart, and is
        exactly how Northstar refunded US$84.00 twice in Chapter 1.

        Args:
            order_id: Order to refund against.
            amount_cents: Integer cents. Must be positive and must not take
                the order's refunded total past its value.
            reason: One of the reasons :meth:`get_policy` knows.
            idempotency_key: Stable key derived from ``(run_id, step_id)``.
                See :func:`northstar_contracts.ids.idempotency_key`.

        Returns:
            The refund record, with ``duplicate`` set when the key was
            already seen.
        """
        args = {
            "order_id": order_id,
            "amount_cents": amount_cents,
            "reason": reason,
            "idempotency_key": idempotency_key,
        }
        self._record_call("issue_refund", args)
        fault = self._take_fault("issue_refund")
        self._pre_write_fault(fault)

        if idempotency_key is not None:
            prior = self._idempotency.get(idempotency_key)
            if prior is not None:
                # The second attempt observes the first attempt's outcome.
                # No new money moves. This single branch is the difference
                # between an incident and a non-event.
                self._post_write_fault(fault)
                return {**prior, "duplicate": True}

        record = self._write_refund(order_id, amount_cents, reason, idempotency_key)

        if fault is not None and fault.kind == "duplicate":
            # An at-least-once delivery path replayed the request. With a
            # key the replay collapses; without one it doubles the money.
            if idempotency_key is None:
                self._write_refund(order_id, amount_cents, reason, None)

        if idempotency_key is not None:
            self._idempotency[idempotency_key] = dict(record)

        self._post_write_fault(fault)
        return {**record, "duplicate": False}

    def send_message(
        self,
        order_id: str,
        body: str,
        channel: str = "email",
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """Send a customer-visible message. Also a write; also unsendable.

        A duplicate refund is recoverable with a clawback. A duplicate
        apology is not: the customer has already read it. Treat messages
        with the same idempotency discipline as money.
        """
        args = {
            "order_id": order_id,
            "channel": channel,
            "idempotency_key": idempotency_key,
        }
        self._record_call("send_message", args)
        fault = self._take_fault("send_message")
        self._pre_write_fault(fault)

        if idempotency_key is not None:
            prior = self._idempotency.get(idempotency_key)
            if prior is not None:
                self._post_write_fault(fault)
                return {**prior, "duplicate": True}

        if order_id not in self.orders:
            raise ToolValidationError(f"no order {order_id!r}")
        if not body.strip():
            raise ToolValidationError("body must not be empty")

        record = {
            "message_id": self._next_id("MSG"),
            "order_id": order_id,
            "channel": channel,
            "body": body,
            "sent_at": self._clock(),
        }
        self.messages.append(record)
        self._record_effect("message_sent", dict(record))
        if idempotency_key is not None:
            self._idempotency[idempotency_key] = dict(record)

        self._post_write_fault(fault)
        return {**record, "duplicate": False}

    def escalate_to_specialist(
        self,
        order_id: str,
        reason: str,
        notes: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """Hand the case to the fraud-review agent.

        Naturally idempotent: a second escalation of the same order returns
        the open case rather than opening another. Some tools you make
        idempotent with a key; some you make idempotent by design, and this
        is the cheaper kind.
        """
        args = {"order_id": order_id, "reason": reason}
        self._record_call("escalate_to_specialist", args)
        fault = self._take_fault("escalate_to_specialist")
        self._pre_write_fault(fault)

        if order_id not in self.orders:
            raise ToolValidationError(f"no order {order_id!r}")

        for case in self.escalations:
            if case["order_id"] == order_id and case["status"] == "open":
                self._post_write_fault(fault)
                return {**case, "duplicate": True}

        record = {
            "case_id": self._next_id("ESC"),
            "order_id": order_id,
            "reason": reason,
            "notes": notes or "",
            "status": "open",
            "queue": "fraud-review",
            "opened_at": self._clock(),
        }
        self.escalations.append(record)
        self._record_effect("escalated", dict(record))
        if idempotency_key is not None:
            self._idempotency[idempotency_key] = dict(record)

        self._post_write_fault(fault)
        return {**record, "duplicate": False}

    # ------------------------------------------------------------ internals

    def _write_refund(
        self,
        order_id: str,
        amount_cents: Money,
        reason: str,
        key: str | None,
    ) -> dict[str, Any]:
        """Validate and apply one refund. The only place money moves."""
        order = self.orders.get(order_id)
        if order is None:
            raise ToolValidationError(f"no order {order_id!r}")
        if not isinstance(amount_cents, int) or isinstance(amount_cents, bool):
            raise ToolValidationError(
                "amount_cents must be an integer number of cents, "
                f"got {type(amount_cents).__name__}"
            )
        if amount_cents <= 0:
            raise ToolValidationError("amount_cents must be positive")

        already = self.total_refunded_cents(order_id)
        if already + amount_cents > order["total_cents"]:
            raise ToolValidationError(
                f"refunding {amount_cents}c would exceed the order value: "
                f"{already}c of {order['total_cents']}c already refunded"
            )

        refund = _Refund(
            refund_id=self._next_id("RFND"),
            order_id=order_id,
            amount_cents=amount_cents,
            reason=reason,
            currency=order["currency"],
            created_at=self._clock(),
            idempotency_key=key,
        )
        self.refunds.append(refund)
        order["refunded_cents"] = already + amount_cents
        record = {
            "refund_id": refund.refund_id,
            "order_id": order_id,
            "amount_cents": amount_cents,
            "currency": refund.currency,
            "reason": reason,
            "created_at": refund.created_at,
        }
        self._record_effect("refund_issued", dict(record))
        return record

    def _guard_read(self, tool: str) -> None:
        """Apply a scheduled fault to a read-only tool."""
        fault = self._take_fault(tool)
        if fault is None:
            return
        if fault.kind == "slow":
            time.sleep(fault.delay_seconds)
            return
        if fault.kind == "timeout":
            raise ToolTimeout(fault.message)
        if fault.kind == "error":
            raise RetryableToolError(fault.message)
        # "duplicate" has no meaning for a read: it returns the same data.

    def _pre_write_fault(self, fault: Fault | None) -> None:
        """Faults that fire before anything is written."""
        if fault is None:
            return
        if fault.kind == "slow":
            time.sleep(fault.delay_seconds)
        elif fault.kind == "error":
            # Nothing landed. This is the *safe* failure: the agent can
            # retry without a key and still be correct.
            raise RetryableToolError(fault.message)

    def _post_write_fault(self, fault: Fault | None) -> None:
        """Faults that fire after the write has landed."""
        if fault is not None and fault.kind == "timeout":
            # The dangerous failure. The effect is durable; the caller will
            # never learn that. Everything the agent does next is a guess
            # unless the call carried an idempotency key.
            raise ToolTimeout(fault.message)

    # ------------------------------------------------------------ tool specs

    def tool_specs(self) -> list[ToolSpec]:
        """The six Northstar tool contracts, in the order agents see them."""
        return list(_TOOL_SPECS)

    def tools(self) -> list[tuple[ToolSpec, Callable[..., Any]]]:
        """Spec/implementation pairs, ready for a ``ToolRegistry``.

        ``ToolRegistry.register_all(world.tools())`` wires the whole
        Northstar surface in one line.
        """
        return [(spec, getattr(self, spec.name)) for spec in _TOOL_SPECS]


def _schema(
    properties: dict[str, Any],
    required: list[str] | None = None,
) -> dict[str, Any]:
    """Build a JSON Schema object with no surprise extra keys allowed."""
    return {
        "type": "object",
        "properties": properties,
        "required": required or [],
        "additionalProperties": False,
    }


_IDEMPOTENCY_PROPERTY = {
    "type": "string",
    "description": (
        "Stable key for this intent. Supply it and a retry is a no-op; "
        "omit it and a retry repeats the effect."
    ),
}

_TOOL_SPECS: tuple[ToolSpec, ...] = (
    ToolSpec(
        name="get_order",
        description=(
            "Look up one Northstar order by id. Returns status, line items, "
            "totals in integer cents, and how much has already been "
            "refunded. Use this before quoting any figure to a customer."
        ),
        input_schema=_schema(
            {"order_id": {"type": "string", "pattern": "^NR-[0-9]{4}-[0-9]{7}$"}},
            ["order_id"],
        ),
        output_schema=_schema(
            {
                "order_id": {"type": "string"},
                "status": {"type": "string"},
                "total_cents": {"type": "integer"},
                "refunded_cents": {"type": "integer"},
                "refundable_cents": {"type": "integer"},
                "items": {"type": "array"},
                "flags": {"type": "array"},
            }
        ),
        writes=False,
        idempotent=True,
        max_result_tokens=600,
    ),
    ToolSpec(
        name="get_policy",
        description=(
            "Return refund eligibility rules by reason and, optionally, by "
            "SKU, plus the approval threshold in cents. Read this before "
            "deciding whether a refund is allowed; do not rely on memory."
        ),
        input_schema=_schema(
            {
                "reason": {
                    "type": "string",
                    "enum": [
                        "damaged",
                        "not_delivered",
                        "changed_mind",
                        "fraud_suspected",
                    ],
                },
                "sku": {"type": "string"},
            }
        ),
        output_schema=_schema(
            {
                "rules": {"type": "array"},
                "approval_threshold_cents": {"type": "integer"},
                "policy_version": {"type": "string"},
            }
        ),
        writes=False,
        idempotent=True,
        max_result_tokens=600,
    ),
    ToolSpec(
        name="search_orders",
        description=(
            "Find orders by customer, status, or flag. Paginated and "
            "token-budgeted: ask for one page at a time and narrow the "
            "filter rather than raising page_size."
        ),
        input_schema=_schema(
            {
                "customer_id": {"type": "string"},
                "status": {"type": "string"},
                "flag": {"type": "string"},
                "page": {"type": "integer", "minimum": 1, "default": 1},
                "page_size": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 10,
                    "default": 2,
                },
                "max_result_tokens": {"type": "integer", "default": 400},
            }
        ),
        output_schema=_schema(
            {
                "results": {"type": "array"},
                "next_page": {"type": ["integer", "null"]},
                "total_matches": {"type": "integer"},
                "truncated": {"type": "boolean"},
            }
        ),
        writes=False,
        idempotent=True,
        max_result_tokens=500,
    ),
    ToolSpec(
        name="issue_refund",
        description=(
            "Refund money against an order. This moves real money and "
            "cannot be undone by calling it again. Always pass "
            "idempotency_key, derived from the run id and step id, so that "
            "a retry after a timeout does not refund twice. Refunds above "
            "the policy threshold require human approval first."
        ),
        input_schema=_schema(
            {
                "order_id": {"type": "string"},
                "amount_cents": {"type": "integer", "minimum": 1},
                "reason": {"type": "string"},
                "idempotency_key": _IDEMPOTENCY_PROPERTY,
            },
            ["order_id", "amount_cents", "reason"],
        ),
        output_schema=_schema(
            {
                "refund_id": {"type": "string"},
                "amount_cents": {"type": "integer"},
                "duplicate": {"type": "boolean"},
            }
        ),
        writes=True,
        idempotent=True,
        max_result_tokens=200,
    ),
    ToolSpec(
        name="send_message",
        description=(
            "Send a message the customer will read. Customer-visible and "
            "irreversible. Pass idempotency_key so a retry does not send "
            "the same apology twice."
        ),
        input_schema=_schema(
            {
                "order_id": {"type": "string"},
                "body": {"type": "string", "minLength": 1},
                "channel": {
                    "type": "string",
                    "enum": ["email", "sms", "in_app"],
                    "default": "email",
                },
                "idempotency_key": _IDEMPOTENCY_PROPERTY,
            },
            ["order_id", "body"],
        ),
        output_schema=_schema(
            {
                "message_id": {"type": "string"},
                "duplicate": {"type": "boolean"},
            }
        ),
        writes=True,
        idempotent=True,
        max_result_tokens=200,
    ),
    ToolSpec(
        name="escalate_to_specialist",
        description=(
            "Hand the case to the fraud-review specialist agent. Use this "
            "for anything flagged fraud_review, and whenever you are not "
            "confident a refund is legitimate. Escalating twice for the "
            "same order reuses the open case."
        ),
        input_schema=_schema(
            {
                "order_id": {"type": "string"},
                "reason": {"type": "string"},
                "notes": {"type": "string"},
                "idempotency_key": _IDEMPOTENCY_PROPERTY,
            },
            ["order_id", "reason"],
        ),
        output_schema=_schema(
            {
                "case_id": {"type": "string"},
                "status": {"type": "string"},
                "duplicate": {"type": "boolean"},
            }
        ),
        writes=True,
        idempotent=True,
        max_result_tokens=200,
    ),
)
