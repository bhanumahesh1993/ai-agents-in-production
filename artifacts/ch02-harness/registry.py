"""Tool dispatch, written out longhand: the five jobs in a fixed order.

Dispatch is where the model's intent becomes an effect on the world, so it
is the natural home for every check you want applied uniformly. Five jobs,
in this order: resolve the spec, validate the arguments against its schema,
ask the policy engine, execute, and normalise whatever comes back.

The one property to take away is that :meth:`HarnessRegistry.dispatch`
never raises. A failed read is information: a model that learns
``get_policy`` returned "no policy for this SKU" can try a different reason
code or escalate, and an exception thrown up through the loop denies it that
chance. Every path out of this function returns a ``ToolResult``, including
the denial path, because every tool-call block must be answered by exactly
one result before the next model call.

``northstar_runtime.ToolRegistry`` is this registry with the production
concerns attached. Read this one first so you know what those are attached
*to*; the fiddly parts, argument validation excepted, are imported rather
than rewritten.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import Any

from northstar_contracts import (
    RetryableToolError,
    ToolCall,
    ToolNotFound,
    ToolResult,
    ToolSpec,
    idempotency_key,
)
from northstar_policy import Decision, PolicyEngine, Principal
from northstar_runtime import truncate_to_budget

__all__ = [
    "HarnessRegistry",
    "is_retryable",
    "normalize",
    "truncate",
    "validate",
]

ToolFn = Callable[..., Any]
ToolBinding = tuple[ToolSpec, ToolFn]

_JSON_TYPES: dict[str, tuple[type, ...]] = {
    "string": (str,),
    "integer": (int,),
    "number": (int, float),
    "boolean": (bool,),
    "array": (list, tuple),
    "object": (dict,),
}


def validate(arguments: dict[str, Any], input_schema: dict[str, Any]) -> list[str]:
    """Check arguments against a JSON Schema. Returns the problems found.

    A small, dependency-free subset: required keys, unknown keys, and
    top-level types. Enough to catch the mistakes models actually make.

    Nothing here coerces. If the model emits ``amount_cents: "32.50"`` the
    honest response is a rejection the model can read and fix. Coercing it
    to 3250 hides a reasoning failure that will recur, and coercing it to 32
    moves the wrong amount of money.
    """
    properties: dict[str, Any] = input_schema.get("properties", {})
    required: list[str] = input_schema.get("required", [])
    problems: list[str] = []

    for key in required:
        if arguments.get(key) is None:
            problems.append(f"{key} is required")

    if input_schema.get("additionalProperties") is False:
        for key in arguments:
            if key not in properties:
                problems.append(
                    f"{key} is not an argument of this tool; valid arguments "
                    f"are {', '.join(sorted(properties))}"
                )

    for key, value in arguments.items():
        expected = properties.get(key, {}).get("type")
        if value is None or not isinstance(expected, str):
            continue
        types = _JSON_TYPES.get(expected)
        if types is None:
            continue
        if expected in ("integer", "number") and isinstance(value, bool):
            # True is an int in Python. It is not an amount in cents.
            problems.append(f"{key} should be {expected}, got boolean")
        elif not isinstance(value, types):
            problems.append(
                f"{key} should be {expected}, got "
                f"{type(value).__name__}: {value!r}"
            )
    return problems


def normalize(exc: BaseException) -> str:
    """Render an exception as a result string you authored.

    Never a traceback. A stack trace in a tool result leaks internal paths,
    dependency versions, and sometimes credentials into the model's context,
    and from there into any summary, memory, or log the run produces. It is
    also an injection surface: an attacker who can influence an error string
    can influence the model.
    """
    return f"{type(exc).__name__}: {exc}"


def is_retryable(exc: BaseException) -> bool:
    """Whether a repeat of this call may succeed.

    Set by code, from a fixed taxonomy, never inferred by the model. A rate
    limit is retryable with backoff. A validation failure is not. An
    authorisation denial is never retryable by escalation. Leaving the
    classification to the model means it changes between runs.

    Retryable is not the same as safe: a timeout on a write is retryable
    only if the write carries an idempotency key. That distinction is
    Chapter 1 in one sentence, and it is why the loop asks
    :meth:`HarnessRegistry.is_retry_safe` and not just this.
    """
    return isinstance(exc, RetryableToolError)


def truncate(value: Any, max_tokens: int) -> tuple[Any, bool]:
    """Shrink a result to its token budget, reporting that it was cut.

    Delegates to ``northstar_runtime.truncate_to_budget``. The flag it
    returns has to reach the result body the model reads, not only your
    logs: a model handed the first 800 tokens of a 40,000-token order search
    with no indication that anything was cut will reason confidently about a
    complete list it never saw.
    """
    return truncate_to_budget(value, max_tokens)


class _ToolTable(dict[str, ToolBinding]):
    """A tool table where an unknown name resolves to a failing tool.

    Dispatch has one error path, not two. Rather than a lookup guard whose
    only job is to build a second kind of failure, a name the model made up
    resolves to a binding that raises :class:`ToolNotFound` when called, so
    the not-found case leaves through ``except Exception`` with every other
    failure. The error text names the tools that do exist, because that text
    is prompt material and a good one recovers the run.
    """

    def __missing__(self, name: str) -> ToolBinding:
        available = ", ".join(sorted(self)) or "(none registered)"

        def absent(**_arguments: Any) -> Any:
            raise ToolNotFound(
                f"no tool named {name!r}. Available tools: {available}."
            )

        spec = ToolSpec(
            name=name,
            description="not registered",
            input_schema={"type": "object", "properties": {}},
            output_schema={"type": "object"},
            writes=False,
            idempotent=True,
        )
        return spec, absent


class HarnessRegistry:
    """Holds tool bindings and dispatches one call at a time.

    Args:
        policy: Decision point asked about every call, before the call
            runs. ``None`` allows everything, which is the right default
            only in a demo.
        principal: Who the run acts as. Passed to the policy engine; it is
            not something the model can put in an argument.
    """

    def __init__(
        self,
        policy: PolicyEngine | None = None,
        principal: Principal | None = None,
    ) -> None:
        self._tools: _ToolTable = _ToolTable()
        self.policy = policy
        self.principal = principal or Principal()
        self.denials: list[ToolCall] = []

    # -- registration -----------------------------------------------------

    def register(self, spec: ToolSpec, fn: ToolFn) -> HarnessRegistry:
        """Register one tool. Returns ``self``, for chaining."""
        if spec.name in self._tools:
            raise ValueError(f"tool {spec.name!r} is already registered")
        self._tools[spec.name] = (spec, fn)
        return self

    def register_all(self, pairs: Iterable[ToolBinding]) -> HarnessRegistry:
        """Register many tools: ``registry.register_all(world.tools())``."""
        for spec, fn in pairs:
            self.register(spec, fn)
        return self

    def specs(self) -> list[ToolSpec]:
        """The contracts the model sees this turn."""
        return [spec for spec, _fn in self._tools.values()]

    def spec_for(self, name: str) -> ToolSpec | None:
        """One spec by name, or ``None`` if it is not registered."""
        binding = dict.get(self._tools, name)
        return binding[0] if binding else None

    def names(self) -> list[str]:
        """Every registered tool name, in registration order."""
        return list(self._tools)

    def bindings(self) -> list[ToolBinding]:
        """Every spec with its implementation, in registration order.

        Lets one registry be rebuilt as another — a registry with a
        different policy, a per-tenant subset, the killable registry the
        resume demo uses — without anything reaching into private state.
        """
        return list(self._tools.values())

    # -- the boundary -----------------------------------------------------

    def stamp(self, call: ToolCall, run_id: str, step_id: str) -> ToolCall:
        """Return ``call`` carrying a derived idempotency key, if it needs one.

        Stamped here, before the intent is journaled, so the journal entry
        records the identity a retry will present. Derived from
        ``(run_id, step_id)`` rather than generated, because a random key
        per attempt is a nonce: it gives the retry a new identity for the
        same intent.
        """
        spec = self.spec_for(call.name)
        if spec is None or not spec.writes:
            return call
        properties = spec.input_schema.get("properties", {})
        if "idempotency_key" not in properties:
            return call
        if call.arguments.get("idempotency_key"):
            return call
        return ToolCall(
            id=call.id,
            name=call.name,
            arguments={
                **call.arguments,
                "idempotency_key": idempotency_key(run_id, step_id),
            },
        )

    def is_retry_safe(self, call: ToolCall) -> bool:
        """Whether repeating this exact call cannot double an effect."""
        spec = self.spec_for(call.name)
        if spec is None:
            return False
        if not spec.writes:
            return True
        if not spec.idempotent:
            return False
        return bool(call.arguments.get("idempotency_key"))

    def authorize(self, spec: ToolSpec, call: ToolCall) -> ToolResult | None:
        """Ask the policy engine. Returns a denial result, or ``None``.

        A denial comes back as a result rather than as an omission. Dropping
        the call would leave the model's ``tool_use`` block unanswered,
        which is a malformed conversation the provider rejects mid-run.
        """
        if self.policy is None:
            return None
        ctx = {"tool": call.name, "writes": spec.writes}
        decision = self.policy.evaluate(self.principal, call, ctx)
        if decision is Decision.ALLOW:
            return None
        self.denials.append(call)
        return ToolResult(
            call_id=call.id,
            ok=False,
            content={
                "error": f"policy returned {decision.value} for {call.name}",
                "retryable": False,
                "decision": decision.value,
            },
        )

    def dispatch(self, call: ToolCall) -> ToolResult:
        """Run one call: validate, authorise, execute, normalise, budget.

        Returns:
            A ``ToolResult``. Always. Failures carry
            ``content={"error": str, "retryable": bool}``.
        """
        spec, fn = self._tools[call.name]
        errors = validate(call.arguments, spec.input_schema)
        if errors:
            return ToolResult(call_id=call.id, ok=False, content={
                "error": f"invalid arguments: {errors}",
                "retryable": False,  # the model must fix the call
            })
        denied = self.authorize(spec, call)   # the third of the five jobs
        if denied is not None:                # the book's excerpt elides
            return denied                     # for space
        try:
            value = fn(**call.arguments)     # runs under the run's
        except Exception as exc:             # principal and scopes
            return ToolResult(call_id=call.id, ok=False, content={
                "error": normalize(exc),        # never a traceback
                "retryable": is_retryable(exc), # code decides this
            })
        value, cut = truncate(value, spec.max_result_tokens)
        return ToolResult(call_id=call.id, ok=True, content=value,
                          truncated=cut)
