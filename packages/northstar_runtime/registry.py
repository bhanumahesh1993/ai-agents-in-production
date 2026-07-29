"""The tool registry: dispatch, validation, budgets, and idempotency.

The registry is the boundary between a model's suggestion and your
production system. Four jobs, in order:

1. **Resolve.** A name the model made up gets an error result naming the
   tools that do exist, because the error message is prompt text and a good
   one recovers the run.
2. **Validate.** Arguments are checked against the declared schema before
   anything runs. A tool that validates its own arguments validates them
   inconsistently.
3. **Stamp.** Write tools that accept an idempotency key get one derived
   from ``(run_id, step, call_id)`` — so a retry, a replay, and a second
   worker all compute the same key.
4. **Budget.** Results are truncated to the tool's ``max_result_tokens``
   and flagged as truncated. An unbounded search result is the most common
   way to blow a context window in production.

A tool that raises never escapes. It becomes a ``ToolResult`` with
``ok=False`` and the model gets to decide what to do about it.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from typing import Any

from northstar_contracts import (
    RetryableToolError,
    ToolCall,
    ToolResult,
    ToolSpec,
    estimate_tokens,
    idempotency_key,
)

__all__ = ["ToolRegistry"]

ToolFn = Callable[..., Any]


class ToolRegistry:
    """Holds tool specs with their implementations and dispatches calls.

    Args:
        inject_idempotency_key: Stamp write tools that declare an
            ``idempotency_key`` argument, when the model did not supply
            one. Default ``False``, so that Chapter 1 can demonstrate the
            unprotected behaviour; :class:`~northstar_runtime.durable.DurableRunner`
            turns it on, because replay without it is not exactly-once.
        validate: Check arguments against ``input_schema`` before calling.
    """

    def __init__(
        self,
        *,
        inject_idempotency_key: bool = False,
        validate: bool = True,
    ) -> None:
        self._specs: dict[str, ToolSpec] = {}
        self._fns: dict[str, ToolFn] = {}
        self.inject_idempotency_key = inject_idempotency_key
        self.validate = validate

    # ----------------------------------------------------------- registration

    def register(self, spec: ToolSpec, fn: ToolFn) -> ToolRegistry:
        """Register one tool. Returns ``self``, for chaining.

        Raises:
            ValueError: On a duplicate name. Two tools with one name is a
                coin flip at dispatch time, and it will land the wrong way
                in production.
        """
        if spec.name in self._specs:
            raise ValueError(f"tool {spec.name!r} is already registered")
        self._specs[spec.name] = spec
        self._fns[spec.name] = fn
        return self

    def register_all(
        self,
        pairs: Iterable[tuple[ToolSpec, ToolFn]],
    ) -> ToolRegistry:
        """Register many tools. ``registry.register_all(world.tools())``."""
        for spec, fn in pairs:
            self.register(spec, fn)
        return self

    def specs(self) -> list[ToolSpec]:
        """Every registered spec, in registration order."""
        return list(self._specs.values())

    def names(self) -> list[str]:
        """Every registered tool name, in registration order."""
        return list(self._specs)

    def spec_for(self, name: str) -> ToolSpec | None:
        """One spec by name, or ``None``."""
        return self._specs.get(name)

    def bindings(self) -> list[tuple[ToolSpec, ToolFn]]:
        """Every spec with its implementation, in registration order.

        Lets one registry be rebuilt as another — a replaying registry, a
        registry with different validation, a per-tenant subset — without
        anything reaching into private state.
        """
        return [(spec, self._fns[name]) for name, spec in self._specs.items()]

    def __contains__(self, name: object) -> bool:
        return name in self._specs

    def __len__(self) -> int:
        return len(self._specs)

    # --------------------------------------------------------------- dispatch

    def dispatch(
        self,
        call: ToolCall,
        run_id: str | None = None,
        step: int | None = None,
    ) -> ToolResult:
        """Run one tool call and return an observation.

        This method does not raise for a tool failure. Ever. A failed tool
        is information the model can act on — retry, try another route, ask
        the customer — and taking that decision away from the loop by
        raising is how a recoverable hiccup becomes a dead run.

        Args:
            call: The call to run.
            run_id: Run this call belongs to. Needed to derive an
                idempotency key.
            step: Step this call belongs to. Same.

        Returns:
            A :class:`~northstar_contracts.models.ToolResult`. Failures
            carry ``content={"error": str, "retryable": bool}``.
        """
        spec = self._specs.get(call.name)
        if spec is None:
            available = ", ".join(self.names()) or "(none registered)"
            return ToolResult.failure(
                call.id,
                f"no tool named {call.name!r}. Available tools: {available}.",
                retryable=False,
            )

        arguments = dict(call.arguments)
        if self.validate:
            problem = _validate(spec, arguments)
            if problem is not None:
                return ToolResult.failure(call.id, problem, retryable=False)

        if self._should_stamp(spec, arguments) and run_id is not None:
            arguments["idempotency_key"] = idempotency_key(
                run_id, f"{step}:{call.id}"
            )

        try:
            content = self._fns[spec.name](**arguments)
        except Exception as exc:  # noqa: BLE001 - the boundary catches all
            return ToolResult.failure(
                call.id,
                f"{type(exc).__name__}: {exc}",
                retryable=isinstance(exc, RetryableToolError),
            )

        content, truncated = truncate_to_budget(
            content, spec.max_result_tokens
        )
        return ToolResult(
            call_id=call.id,
            ok=True,
            content=content,
            truncated=truncated,
        )

    def is_retry_safe(self, call: ToolCall) -> bool:
        """Whether repeating this exact call cannot double an effect.

        The question a harness must answer before retrying a timeout, and
        the question Northstar got wrong in Chapter 1. Three cases:

        * a read is always safe;
        * a write declared non-idempotent is never safe;
        * a write declared idempotent is safe **only if the call actually
          carries an idempotency key**. The declaration on its own is a
          promise about keyed calls, not about all calls.

        Returns:
            ``True`` if the runtime may retry this call unchanged.
        """
        spec = self._specs.get(call.name)
        if spec is None:
            return False
        if not spec.writes:
            return True
        if not spec.idempotent:
            return False
        if call.arguments.get("idempotency_key"):
            return True
        return self._should_stamp(spec, dict(call.arguments))

    def _should_stamp(self, spec: ToolSpec, arguments: dict[str, Any]) -> bool:
        """Whether to derive an idempotency key for this call."""
        if not self.inject_idempotency_key or not spec.writes:
            return False
        if "idempotency_key" not in spec.input_schema.get("properties", {}):
            return False
        return not arguments.get("idempotency_key")


def _validate(spec: ToolSpec, arguments: dict[str, Any]) -> str | None:
    """Check arguments against the declared schema.

    A small, dependency-free subset of JSON Schema: required keys, unknown
    keys, and top-level types. Enough to catch the mistakes models actually
    make, and short enough to read. Swap in a real validator when the
    schemas grow past this.

    Returns:
        An error message written for the model to act on, or ``None``.
    """
    schema = spec.input_schema
    properties: dict[str, Any] = schema.get("properties", {})
    required: list[str] = schema.get("required", [])

    missing = [k for k in required if arguments.get(k) is None]
    if missing:
        return (
            f"{spec.name} is missing required argument(s): "
            f"{', '.join(sorted(missing))}. "
            f"Expected: {', '.join(sorted(properties)) or '(none)'}."
        )

    if schema.get("additionalProperties") is False:
        unknown = [k for k in arguments if k not in properties]
        if unknown:
            return (
                f"{spec.name} got unknown argument(s): "
                f"{', '.join(sorted(unknown))}. "
                f"Valid arguments: {', '.join(sorted(properties))}."
            )

    for key, value in arguments.items():
        expected = properties.get(key, {}).get("type")
        if value is None or not isinstance(expected, str):
            continue
        if not _type_ok(expected, value):
            actual = type(value).__name__
            return (
                f"{spec.name} argument {key!r} should be {expected}, "
                f"got {actual}: {value!r}."
            )
    return None


_JSON_TYPES: dict[str, tuple[type, ...]] = {
    "string": (str,),
    "integer": (int,),
    "number": (int, float),
    "boolean": (bool,),
    "array": (list, tuple),
    "object": (dict,),
}


def _type_ok(expected: str, value: Any) -> bool:
    """Whether ``value`` satisfies a JSON Schema primitive type."""
    types = _JSON_TYPES.get(expected)
    if types is None:
        return True
    if expected in ("integer", "number") and isinstance(value, bool):
        # ``True`` is an int in Python. It is not an amount in cents.
        return False
    return isinstance(value, types)


def truncate_to_budget(
    content: Any,
    max_tokens: int,
) -> tuple[Any, bool]:
    """Shrink a tool result to fit its token budget.

    Two strategies, in order of how much they preserve:

    1. If the result is a dict holding a list, drop items from the end
       until it fits and record how many were dropped. The shape survives,
       so the model can still parse it and can ask for the next page.
    2. Otherwise, replace it with a preview and a note. Lossy, but bounded
       — and bounded beats faithful when the alternative is a context
       window full of one tool's output.

    Args:
        content: The tool's return value.
        max_tokens: Budget from the tool's spec.

    Returns:
        ``(content, truncated)``.
    """
    if estimate_tokens(content) <= max_tokens:
        return content, False

    if isinstance(content, dict):
        for key, value in content.items():
            if not isinstance(value, list) or not value:
                continue
            kept = list(value)
            while kept and estimate_tokens({**content, key: kept}) > max_tokens:
                kept.pop()
            shrunk = {
                **content,
                key: kept,
                "truncated": True,
                "omitted_items": len(value) - len(kept),
                "truncation_note": (
                    f"{len(value) - len(kept)} item(s) omitted to fit a "
                    f"{max_tokens}-token budget. Narrow the query or "
                    f"request the next page."
                ),
            }
            return shrunk, True

    preview = str(content)[: max_tokens * 4]
    return (
        {
            "truncated": True,
            "preview": preview,
            "truncation_note": (
                f"Result exceeded its {max_tokens}-token budget and was "
                f"cut. Ask for a narrower slice."
            ),
        },
        True,
    )


def specs_of(pairs: Sequence[tuple[ToolSpec, ToolFn]]) -> list[ToolSpec]:
    """Pull the specs out of spec/implementation pairs."""
    return [spec for spec, _ in pairs]
