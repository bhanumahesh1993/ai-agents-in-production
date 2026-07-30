"""The Northstar tool library, as contracts rather than as functions.

Eight contracts. Six of them are the tools the book has used since Chapter 1;
``preview_refund`` and ``run_code`` are new here. Every field in every
``ToolSpec`` is read by something at runtime, which is the whole argument of
the chapter: a flag humans read is a convention, and a flag in the contract
is a control.

Three decisions in this file are worth reading before the schemas.

**The description is prompt text.** It is resident in the context window,
re-read on every turn of every run, and it is the primary evidence the model
uses to decide whether this tool is the right one. So a description change is
a behaviour change: it goes through the evaluation gate, not the
documentation gate, and it is part of the version. ``SEARCH_ORDERS_DRIFTED``
at the bottom of this file is the four-line diff from the opening incident,
kept as a fixture precisely so a test can fail on it.

**Three narrow tools beat one broad one.** ``get_order``,
``preview_refund``, and ``issue_refund`` are individually authorizable,
individually testable, and individually auditable. One ``run_sql`` is none of
those, because its real permission set is "whatever that interface can
reach". :data:`BROAD_CAPABILITIES` names the ones the conformance suite
refuses outright.

**The dry run is its own tool.** ``preview_refund`` has ``writes=False``, so
a read-only agent can run it, a policy engine can allow it unconditionally,
and an approval payload can be built from its output. A ``dry_run=True`` flag
on ``issue_refund`` gives none of those, because the tool is still registered
as a write and still authorized as one.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from northstar_contracts import Money, ToolSpec

__all__ = [
    "APPROVAL_REQUIRED",
    "BROAD_CAPABILITIES",
    "COMPENSATIONS",
    "DEFAULT_MAX_RESULT_TOKENS",
    "ESCALATE_TO_SPECIALIST",
    "GET_ORDER",
    "GET_POLICY",
    "ISSUE_REFUND",
    "MAX_REFUND_CENTS",
    "PREVIEW_REFUND",
    "REFUND_INPUT",
    "REFUND_OUTPUT",
    "REFUND_REASONS",
    "RUN_CODE",
    "SEARCH_ORDERS",
    "SEARCH_ORDERS_DRIFTED",
    "SEND_MESSAGE",
    "SPECS",
    "Compensation",
    "compensation_for",
    "schema",
    "spec_for",
]

#: The book's default result budget. ``issue_refund`` sets 200, because a
#: receipt is small and anything larger means the tool is returning
#: something it should not.
DEFAULT_MAX_RESULT_TOKENS = 800

#: A bound that fails closed. A description mentioning a limit is a
#: suggestion; a schema that rejects a 900,000-cent refund is a control.
MAX_REFUND_CENTS: Money = 100000

#: The caller's vocabulary for why a refund is being asked for. An enum,
#: not a string: a ``reason`` typed as ``string`` invites the model to
#: invent a taxonomy, and then nothing can count error classes across runs.
REFUND_REASONS: tuple[str, ...] = (
    "damaged",
    "not_received",
    "wrong_item",
    "changed_mind",
)

#: Names that transfer more authority than any single task needs. Refused by
#: the conformance suite rather than reviewed case by case, because once
#: granted this authority cannot be scoped, audited, or reasoned about.
BROAD_CAPABILITIES: frozenset[str] = frozenset(
    {"run_sql", "call_api", "execute_shell", "eval", "query_db", "http_get"}
)


def schema(
    properties: dict[str, Any],
    required: list[str] | None = None,
) -> dict[str, Any]:
    """A strict JSON Schema object: no extra properties, ever.

    ``additionalProperties: false`` is where most reliability is bought
    cheaply. Without it a model that invents an argument gets a silent
    success against a tool that ignored it.
    """
    return {
        "type": "object",
        "properties": properties,
        "required": required or [],
        "additionalProperties": False,
    }


# ------------------------------------------------------------ compensation


@dataclass(frozen=True)
class Compensation:
    """A declared inverse operation, with the window it is meaningful in.

    Written next to the tool it inverts so that three things become
    possible: a human reviewing an approval can be told whether the action
    is reversible and for how long, a supervisor can undo a bad step without
    inventing a procedure, and a design review can see which actions have no
    inverse at all.

    That last set is the important output. Actions with neither idempotency
    nor compensation are exactly the ones that must sit behind human
    approval, because approval is the only remaining control.

    Args:
        tool: The inverse operation's name.
        window: How long the inverse is meaningful.
        cost: What running it actually costs. Not always money.
        restores: ``True`` if the world returns to its prior state.
            ``False`` for ``send_message``: a duplicate apology has already
            been read, and a correction message costs customer trust rather
            than money, and should be priced as such.
    """

    tool: str
    window: str
    cost: str
    restores: bool = True


#: Every write in this library, and what undoes it.
COMPENSATIONS: dict[str, Compensation] = {
    "issue_refund": Compensation(
        tool="cancel_refund",
        window="the settlement window, 72 hours",
        cost="the money returns; the customer sees a reversal",
    ),
    "send_message": Compensation(
        tool="send_correction",
        window="none: delivery is final",
        cost="customer trust",
        restores=False,
    ),
    "escalate_to_specialist": Compensation(
        tool="withdraw_escalation",
        window="while the case is open",
        cost="a specialist's time",
    ),
}

#: Writes with no compensation at all. The conformance suite requires this
#: set and the compensation table between them to cover every write, so the
#: set of irreversible *unattended* actions is empty by construction rather
#: than by hope.
APPROVAL_REQUIRED: frozenset[str] = frozenset({"run_code"})


def compensation_for(tool: str) -> Compensation | None:
    """The declared inverse of ``tool``, or ``None`` if it has none."""
    return COMPENSATIONS.get(tool)


# ----------------------------------------------------------------- schemas

_IDEMPOTENCY_KEY = {
    "type": "string",
    "minLength": 16,
    "description": (
        "Derived from the run id and step id, never generated per attempt. "
        "Retrying with the same key returns the original receipt."
    ),
}

REFUND_INPUT = schema(
    {
        "order_id": {
            "type": "string",
            "pattern": "^NR-[0-9]{4}-[0-9]{7}$",
        },
        "amount_cents": {
            "type": "integer",
            "minimum": 1,
            "maximum": MAX_REFUND_CENTS,
        },
        "reason": {"type": "string", "enum": list(REFUND_REASONS)},
        "idempotency_key": _IDEMPOTENCY_KEY,
    },
    ["order_id", "amount_cents", "reason", "idempotency_key"],
)

#: Receipt id, amount in cents, status. Nothing else: the tool's job is to
#: say what happened to the money, and a declared output shape is what lets
#: the result be truncated safely, lets a state grader assert on a receipt
#: rather than on prose, and lets the lint compute a size before the result
#: reaches the model.
REFUND_OUTPUT = schema(
    {
        "receipt_id": {"type": "string"},
        "amount_cents": {"type": "integer"},
        "status": {
            "type": "string",
            "enum": ["settled", "duplicate"],
        },
    },
    ["receipt_id", "amount_cents", "status"],
)

PREVIEW_OUTPUT = schema(
    {
        "order_id": {"type": "string"},
        "amount_cents": {"type": "integer"},
        "refundable_cents": {"type": "integer"},
        "policy_clauses": {"type": "array"},
        "resulting_status": {"type": "string"},
        "requires_approval": {"type": "boolean"},
        "reversible_for": {"type": "string"},
    },
    ["order_id", "amount_cents", "requires_approval"],
)

#: The item schema is declared, not left as a bare array. That is the whole
#: difference between shaping and not shaping: a refund decision needs the
#: SKU, the quantity, the unit price in cents, and the name to quote back. It
#: does not need the carrier's tracking events, the marketing attribution
#: block, or the forty-field customer profile the orders API returns because
#: some other consumer needed it once.
ITEM_OUTPUT = schema(
    {
        "sku": {"type": "string"},
        "name": {"type": "string"},
        "quantity": {"type": "integer"},
        "unit_price_cents": {"type": "integer"},
    }
)

ORDER_OUTPUT = schema(
    {
        "order_id": {"type": "string"},
        "status": {"type": "string"},
        "total_cents": {"type": "integer"},
        "refunded_cents": {"type": "integer"},
        "refundable_cents": {"type": "integer"},
        "items": {"type": "array", "items": ITEM_OUTPUT},
    },
    ["order_id", "status", "total_cents"],
)

#: One search row: an identifier plus one line of context. The row schema is
#: what makes an unshaped upstream payload shrink rather than merely get cut.
SEARCH_ROW_OUTPUT = schema(
    {
        "order_id": {"type": "string"},
        "status": {"type": "string"},
        "total_cents": {"type": "integer"},
        "item_count": {"type": "integer"},
        "flags": {"type": "array"},
    }
)

SEARCH_OUTPUT = schema(
    {
        "results": {"type": "array", "items": SEARCH_ROW_OUTPUT},
        "total_matches": {"type": "integer"},
        "next_page": {"type": ["integer", "null"]},
        "truncated": {"type": "boolean"},
        "cursor": {"type": "string"},
        "note": {"type": "string"},
    },
    ["results"],
)


# ------------------------------------------------------------------- specs


GET_ORDER = ToolSpec(
    name="get_order",
    description=(
        "Look up one Northstar order by id. Use this when you need the "
        "item-level breakdown, which no search result carries. Returns the "
        "complete record: status, line items with SKUs and unit prices in "
        "integer cents, the order total, how much has already been "
        "refunded, and how much remains refundable. Does not change "
        "anything and does not quote policy; call get_policy for that."
    ),
    input_schema=schema(
        {"order_id": {"type": "string", "pattern": "^NR-[0-9]{4}-[0-9]{7}$"}},
        ["order_id"],
    ),
    output_schema=ORDER_OUTPUT,
    writes=False,
    idempotent=True,
    max_result_tokens=600,
    version="2",
)

GET_POLICY = ToolSpec(
    name="get_policy",
    description=(
        "Return refund eligibility rules by reason and, optionally, by SKU. "
        "Use this when deciding whether a refund is allowed at all, before "
        "any preview or commit; do not rely on memory, because the rules "
        "change without the tool changing. Returns the matching rules, the "
        "approval threshold in integer cents, and the policy version. Does "
        "not decide the amount and does not grant an approval."
    ),
    input_schema=schema(
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
    output_schema=schema(
        {
            "rules": {"type": "array"},
            "approval_threshold_cents": {"type": "integer"},
            "policy_version": {"type": "string"},
        },
        ["rules", "approval_threshold_cents"],
    ),
    writes=False,
    idempotent=True,
    max_result_tokens=600,
    version="2",
)

#: The description as it was *before* the four-line diff. Note the two
#: sentences the incident deleted: the one saying the rows are partial, and
#: the one saying what to call next.
SEARCH_ORDERS = ToolSpec(
    name="search_orders",
    description=(
        "Search orders by customer, status, or flag. Use this when you do "
        "not already know the order id; narrow the filter rather than "
        "raising page_size. Returns a paginated list of matching order ids "
        "with status and total in integer cents. Rows are summaries: they "
        "carry no item-level breakdown, so call get_order for the full "
        "record before quoting or refunding any single item. Does not "
        "change anything."
    ),
    input_schema=schema(
        {
            "customer_id": {"type": "string"},
            "status": {"type": "string"},
            "flag": {"type": "string"},
            "page": {"type": "integer", "minimum": 1},
            "page_size": {"type": "integer", "minimum": 1, "maximum": 10},
        }
    ),
    output_schema=SEARCH_OUTPUT,
    writes=False,
    idempotent=True,
    max_result_tokens=800,
    version="4",
)

#: The four-line diff, as a fixture. Labelled `documentation only`, shipped
#: without a test, and it stopped the agent calling ``get_order``.
SEARCH_ORDERS_DRIFTED = (
    "Search orders. Use this when you need a customer's orders. Returns "
    "matching orders with status and totals. Does not change anything."
)

PREVIEW_REFUND = ToolSpec(
    name="preview_refund",
    description=(
        "Show exactly what issuing this refund would do, without doing it. "
        "Use this when you have an amount in mind and before every call to "
        "issue_refund, and use it freely: it is a read. Returns the amount "
        "in integer cents, the refundable balance, the policy clauses that "
        "permit it, the status the order would end in, whether it would "
        "require human approval, and how long it would stay reversible. "
        "Does not move money, does not reserve anything, and does not "
        "create an approval request."
    ),
    input_schema=schema(
        {
            "order_id": {
                "type": "string",
                "pattern": "^NR-[0-9]{4}-[0-9]{7}$",
            },
            "amount_cents": {
                "type": "integer",
                "minimum": 1,
                "maximum": MAX_REFUND_CENTS,
            },
            "reason": {"type": "string", "enum": list(REFUND_REASONS)},
        },
        ["order_id", "amount_cents", "reason"],
    ),
    output_schema=PREVIEW_OUTPUT,
    writes=False,
    idempotent=True,
    max_result_tokens=400,
    version="1",
)

ISSUE_REFUND = ToolSpec(
    name="issue_refund",
    description=(
        "Issue a refund against a delivered order. Use this when a "
        "preview_refund result exists and, above the approval threshold, a "
        "human has approved that exact call. Amount is in integer cents. "
        "Requires an idempotency_key: retrying with the same key returns "
        "the original receipt and moves no additional money. Call "
        "get_policy first. Returns the receipt id, the amount in cents, "
        "and the settlement status. Does not cancel orders, does not "
        "email the customer."
    ),
    input_schema=REFUND_INPUT,     # strict JSON Schema, enums, bounds
    output_schema=REFUND_OUTPUT,   # receipt id, amount_cents, status
    writes=True,
    idempotent=True,               # true only with idempotency_key
    max_result_tokens=200,
    version="3",
)

SEND_MESSAGE = ToolSpec(
    name="send_message",
    description=(
        "Send a message the customer will read. Use this when the case is "
        "resolved or when you need something from the customer. Irreversible: "
        "a correction message is the only compensation and it costs trust. "
        "Requires an idempotency_key so a retry does not send the same "
        "apology twice. Returns the message id and whether the send was a "
        "duplicate. Does not refund anything and does not close the case."
    ),
    input_schema=schema(
        {
            "order_id": {
                "type": "string",
                "pattern": "^NR-[0-9]{4}-[0-9]{7}$",
            },
            "body": {"type": "string", "minLength": 1, "maxLength": 2000},
            "channel": {
                "type": "string",
                "enum": ["email", "sms", "in_app"],
            },
            "idempotency_key": _IDEMPOTENCY_KEY,
        },
        ["order_id", "body", "idempotency_key"],
    ),
    output_schema=schema(
        {
            "message_id": {"type": "string"},
            "status": {"type": "string", "enum": ["sent", "duplicate"]},
        },
        ["message_id", "status"],
    ),
    writes=True,
    idempotent=True,
    max_result_tokens=200,
    version="2",
)

ESCALATE_TO_SPECIALIST = ToolSpec(
    name="escalate_to_specialist",
    description=(
        "Hand the case to the fraud review specialist. Use this when the "
        "order is flagged fraud_review, when the reason is fraud_suspected, "
        "and whenever you are not confident a refund is legitimate. "
        "Escalating the same order twice reuses the open case. Requires an "
        "idempotency_key. Returns the case id and its status. Does not "
        "refund anything and does not message the customer."
    ),
    input_schema=schema(
        {
            "order_id": {
                "type": "string",
                "pattern": "^NR-[0-9]{4}-[0-9]{7}$",
            },
            "reason": {
                "type": "string",
                "enum": [*REFUND_REASONS, "fraud_suspected"],
            },
            "notes": {"type": "string", "maxLength": 500},
            "idempotency_key": _IDEMPOTENCY_KEY,
        },
        ["order_id", "reason", "idempotency_key"],
    ),
    output_schema=schema(
        {
            "case_id": {"type": "string"},
            "status": {"type": "string"},
        },
        ["case_id", "status"],
    ),
    writes=True,
    idempotent=True,
    max_result_tokens=200,
    version="2",
)

#: Code execution does not add one risk. It converts every other risk into a
#: larger one, because it dissolves the contract the rest of this file is
#: about: the input schema becomes "any program", the ``writes`` flag stops
#: being knowable before the call, the result budget is whatever the program
#: prints, and idempotency is undefined. So ``writes=True`` and
#: ``idempotent=False`` are the honest declarations, and the contract that
#: matters lives outside the code, in
#: :class:`sandbox.SandboxContract`.
RUN_CODE = ToolSpec(
    name="run_code",
    description=(
        "Run a short Python program over data you pass in, in a sandbox "
        "with no network and no credentials. Use this when you need to "
        "filter, join, or aggregate more rows than belong in the "
        "conversation, and never for anything a typed tool already does. "
        "Returns whatever the program prints, capped, plus the wall time it "
        "took. Does not reach the network, does not read the filesystem, "
        "does not persist between calls, and cannot call any other tool."
    ),
    input_schema=schema(
        {
            "program": {"type": "string", "maxLength": 4000},
            "inputs": {"type": "object"},
        },
        ["program"],
    ),
    output_schema=schema(
        {
            "stdout": {"type": "string"},
            "wall_seconds": {"type": "number"},
            "truncated": {"type": "boolean"},
        },
        ["stdout"],
    ),
    writes=True,
    idempotent=False,
    max_result_tokens=400,
    version="1",
)

#: The library, in the order agents see it. Reads first, then the preview,
#: then the writes: the read/write split is visible in the list itself.
SPECS: dict[str, ToolSpec] = {
    spec.name: spec
    for spec in (
        GET_ORDER,
        GET_POLICY,
        SEARCH_ORDERS,
        PREVIEW_REFUND,
        ISSUE_REFUND,
        SEND_MESSAGE,
        ESCALATE_TO_SPECIALIST,
        RUN_CODE,
    )
}


def spec_for(name: str) -> ToolSpec:
    """One spec by name.

    Raises:
        KeyError: With the library's names in the message, because the
            error is prompt text too.
    """
    if name not in SPECS:
        known = ", ".join(SPECS)
        raise KeyError(f"no tool named {name!r}. This library has: {known}.")
    return SPECS[name]
