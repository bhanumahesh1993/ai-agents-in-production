"""Result budgets: shape, then truncate, then always declare truncation.

A tool that returns 40,000 tokens of JSON is not returning a big result. It
is a design defect with three distinct costs. It is billed on this turn and
on every subsequent turn, because the accumulated history is re-sent. It
crowds out the content the agent actually needs. And it is unbounded by
construction: the size is a function of the data, so a run that worked in
staging against forty orders fails in production against four thousand.

The fix is to make the result size part of the contract, and to enforce it
mechanically. Three properties of :func:`enforce_budget` matter more than its
mechanics.

It **shapes before it truncates**, dropping fields the output schema does not
declare, so the budget is spent on fields somebody chose rather than on
whatever the upstream API happened to include. Shaping is where the tool
author earns their keep, because the tool author is the only person who knows
which fields matter: a refund decision needs the order's status, the item
lines with SKUs and prices in cents, and the delivery date. It does not need
the carrier's internal tracking events, the marketing attribution block, or
the forty-field customer profile the orders API returns because some other
consumer needed it once.

It **always sets ``truncated``**, because a truncated result that does not say
so is a correctness bug rather than a cosmetic one: the agent reasons about a
partial list as though it were complete, and concludes that the order it was
looking for does not exist.

And it **hands back a way to continue**, so truncation is a normal
interaction rather than a dead end.
"""

from __future__ import annotations

from typing import Any

from northstar_contracts import ToolResult, ToolSpec, estimate_tokens

__all__ = [
    "CURSOR_TOKENS",
    "count_tokens",
    "enforce_budget",
    "fit",
    "shape",
]

#: Room reserved for the cursor and the truncation note. Budgeting to the
#: exact cap and then adding the note is how a result ends up over its cap
#: while claiming to be under it.
CURSOR_TOKENS = 40


def count_tokens(payload: Any) -> int:
    """Estimate the token cost of a result.

    Delegates to :func:`northstar_contracts.tokens.estimate_tokens`, which is
    a deterministic four-characters-per-token approximation. Budgeting needs
    a number that is stable, offline, and identical on every machine, not one
    that is exact. Swap it for a real tokeniser when you bill on it.
    """
    return estimate_tokens(payload)


def shape(payload: dict[str, Any], output_schema: dict[str, Any]) -> Any:
    """Keep only what the output schema declares, recursively.

    Args:
        payload: Whatever the tool returned.
        output_schema: The declared shape. An empty schema shapes nothing,
            which is why the conformance suite requires one: without it the
            budget is spent on whatever the upstream API felt like sending.

    Returns:
        The payload with undeclared keys dropped. Arrays keep their items and
        each item is shaped against ``items.properties`` when the schema
        declares one, so a search result's rows get trimmed too.
    """
    properties: dict[str, Any] = output_schema.get("properties", {})
    if not properties:
        return payload
    shaped: dict[str, Any] = {}
    for key, declared in properties.items():
        if key not in payload:
            continue
        value = payload[key]
        if declared.get("type") == "array" and isinstance(value, list):
            item_schema = declared.get("items")
            if isinstance(item_schema, dict) and item_schema.get("properties"):
                value = [
                    shape(v, item_schema) if isinstance(v, dict) else v
                    for v in value
                ]
        shaped[key] = value
    return shaped


def fit(shaped: dict[str, Any], budget: int) -> dict[str, Any]:
    """Cut a shaped result down to ``budget`` tokens, leaving a cursor.

    Two strategies, in order of how much they preserve. If the result holds a
    list, items are dropped from the end until it fits, so the shape survives
    and the model can still parse it and ask for the next page. Otherwise the
    result is replaced by a bounded preview, which is lossy -- and bounded
    beats faithful when the alternative is a context window full of one
    tool's output.

    Args:
        shaped: The result, already shaped to its output schema.
        budget: Tokens available for the content, cursor excluded.

    Returns:
        A dict carrying ``cursor``, and either the surviving list or a
        ``preview``. The caller adds the note and sets ``truncated``.
    """
    for key, value in shaped.items():
        if not isinstance(value, list) or not value:
            continue
        kept = list(value)
        while kept and count_tokens({**shaped, key: kept}) > budget:
            kept.pop()
        return {
            **shaped,
            key: kept,
            "cursor": f"{key}:{len(kept)}",
            "omitted_items": len(value) - len(kept),
        }
    preview = str(shaped)[: max(0, budget) * 4]
    return {"preview": preview, "cursor": "preview:0", "omitted_items": 0}


def enforce_budget(spec: ToolSpec, payload: dict[str, Any]) -> ToolResult:
    """Shape, then truncate, then always declare truncation.

    Args:
        spec: The tool's contract. ``output_schema`` drives the shaping and
            ``max_result_tokens`` is the cap.
        payload: The tool's return value, plus a ``call_id``. The call id is
            correlation metadata rather than content, so it is read here and
            then dropped by shaping, which is why it must not appear in the
            output schema.

    Returns:
        A :class:`~northstar_contracts.models.ToolResult`. Over-cap results
        carry ``truncated=True``, a ``cursor``, and a note telling the model
        how to continue.
    """
    shaped = shape(payload, spec.output_schema)   # drop extras
    if count_tokens(shaped) <= spec.max_result_tokens:
        return ToolResult(call_id=payload["call_id"], ok=True,
                          content=shaped, truncated=False)
    head = fit(shaped, spec.max_result_tokens - CURSOR_TOKENS)
    head["note"] = (
        f"Truncated to {spec.max_result_tokens} tokens. "
        f"Call again with cursor={head['cursor']!r} for more."
    )
    return ToolResult(call_id=payload["call_id"], ok=True,
                      content=head, truncated=True)
