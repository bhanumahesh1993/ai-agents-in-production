"""The conformance suite: the chapter as executable rules.

This is the piece worth copying into your own repository, because it turns
prose about tool quality into a build failure.
:meth:`ConformingRegistry.register` calls :func:`check`, so a tool that fails
cannot be registered at all -- which is a stronger guarantee than a test,
since a test can be skipped and a registration cannot.

Two of the rules deserve naming.

The ``writes and idempotent`` rule catches the most dangerous lie a contract
can tell, which is a tool that claims deduplication it does not implement.
``issue_refund`` is idempotent *with* an ``idempotency_key`` and not
otherwise, so the schema has to make the key required. An ``idempotent=True``
that is not enforced downstream is a false safety claim on which retry logic
will be built.

The compensation rule does not require every write to be reversible. It
requires every write to be *either* reversible *or* explicitly listed in the
approval policy, so that the set of irreversible unattended actions is empty by
construction rather than by hope.

Two rules operate on the library rather than on one tool, and live in
:func:`check_library`: the overlap test, which reads every pair of descriptions
and asks whether a reasonable reader could pick either for the same request,
and the dry-run rule, which requires every mutating tool with a preview to
have that preview registered as its own read.
"""

from __future__ import annotations

import inspect
import re
from collections.abc import Callable
from dataclasses import replace
from typing import Any

from northstar_contracts import ToolCall, ToolResult, ToolSpec, idempotency_key
from northstar_runtime import ToolRegistry
from specs import (
    APPROVAL_REQUIRED,
    BROAD_CAPABILITIES,
    compensation_for,
)

__all__ = [
    "DESCRIPTION_BUDGET",
    "NAME_RE",
    "REQUIRED_SECTIONS",
    "ConformanceError",
    "ConformingRegistry",
    "check",
    "check_library",
    "required",
]

#: ``snake_case``, verb then noun, at least two words. The model matches
#: intent against names before it reads a word of the description, and an
#: ambiguous name costs a wrong call that a perfect description will not
#: always undo.
NAME_RE = re.compile(r"^[a-z][a-z0-9]*(?:_[a-z0-9]+)+$")

#: Descriptions are billed on every turn. Thirty tools at 400 tokens each is
#: 12,000 tokens of standing overhead before the conversation starts.
DESCRIPTION_BUDGET = 900

#: The three questions a description must answer in words a grep can find.
#: "What does it not do" is the sentence teams skip and the model needs most,
#: because the space of things a tool plausibly might do is much larger than
#: the space of things it does.
REQUIRED_SECTIONS: tuple[str, ...] = ("Returns", "Use this when", "Does not")


class ConformanceError(ValueError):
    """A tool that may not be registered.

    Raised by :meth:`ConformingRegistry.register`, with every problem listed
    at once rather than one per run, because fixing a contract is one editing
    session and not six.
    """


def required(schema: dict[str, Any]) -> list[str]:
    """The schema's required field names."""
    return [str(k) for k in schema.get("required", [])]


def check(spec: ToolSpec, fn: Callable[..., Any]) -> list[str]:
    """Every problem with one tool contract. Empty means it may ship.

    Args:
        spec: The contract.
        fn: The implementation. Checked against the schema, because a schema
            the function does not honour is documentation again: the registry
            calls ``fn(**arguments)``, so a required field the signature
            cannot accept is a ``TypeError`` at the worst possible moment.

    Returns:
        Problems, in the order the chapter introduces them.
    """
    p: list[str] = []
    if not NAME_RE.match(spec.name):
        p.append("name must be snake_case verb_noun")
    if spec.name in BROAD_CAPABILITIES:
        p.append(
            f"{spec.name!r} is a broad capability: its real permission set "
            "is whatever its interface can reach. Ship narrow domain "
            "operations instead."
        )
    if len(spec.description) > DESCRIPTION_BUDGET:
        p.append(f"description over budget ({DESCRIPTION_BUDGET} chars)")
    for q in REQUIRED_SECTIONS:
        if q not in spec.description:
            p.append(f"description missing section: {q}")
    if spec.input_schema.get("additionalProperties") is not False:
        p.append("input_schema must forbid extra properties")
    if not spec.output_schema:
        p.append("output_schema is required")
    if not spec.output_schema.get("properties"):
        p.append(
            "output_schema declares no properties, so nothing can be shaped, "
            "truncated safely, redacted, or graded"
        )
    if "call_id" in spec.output_schema.get("properties", {}):
        p.append(
            "output_schema declares call_id: correlation metadata is not "
            "content and shaping must drop it"
        )
    if spec.writes and spec.idempotent:
        if "idempotency_key" not in required(spec.input_schema):
            p.append("idempotent write must require a key")
    if spec.writes and not compensation_for(spec.name):
        if spec.name not in APPROVAL_REQUIRED:
            p.append("write with no compensation and no approval rule")
    if spec.max_result_tokens <= 0:
        p.append("max_result_tokens must be positive")
    if not spec.version:
        p.append("version is required: a resumed run binds to its pinned one")
    p.extend(_signature_problems(spec, fn))
    return p


def _signature_problems(
    spec: ToolSpec,
    fn: Callable[..., Any],
) -> list[str]:
    """Whether the implementation can actually be called from the schema.

    The registry dispatches ``fn(**arguments)`` after validating against the
    input schema. So the function must accept every declared property and must
    not require anything the schema does not declare, or a call the schema
    called valid raises a ``TypeError`` in production.
    """
    problems: list[str] = []
    try:
        signature = inspect.signature(fn)
    except (TypeError, ValueError):  # pragma: no cover - builtins only
        return ["implementation has no inspectable signature"]

    accepts_kwargs = any(
        param.kind is inspect.Parameter.VAR_KEYWORD
        for param in signature.parameters.values()
    )
    named = {
        name
        for name, param in signature.parameters.items()
        if param.kind
        in (
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
            inspect.Parameter.KEYWORD_ONLY,
        )
    }
    declared = set(spec.input_schema.get("properties", {}))
    if not accepts_kwargs:
        missing = sorted(declared - named)
        if missing:
            problems.append(
                f"implementation cannot accept declared argument(s): "
                f"{', '.join(missing)}"
            )
    unfilled = sorted(
        name
        for name, param in signature.parameters.items()
        if param.default is inspect.Parameter.empty
        and param.kind
        in (
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
            inspect.Parameter.KEYWORD_ONLY,
        )
        and name not in declared
    )
    if unfilled:
        problems.append(
            f"implementation requires argument(s) the schema does not "
            f"declare: {', '.join(unfilled)}"
        )
    return problems


def check_library(specs: list[ToolSpec]) -> list[str]:
    """Problems that only exist between tools.

    Two rules.

    **The overlap test.** Read every pair of descriptions and ask whether a
    reasonable reader could pick either one for the same request. If the
    answer is yes, the model will sometimes pick the wrong one, and no amount
    of system-prompt instruction reliably fixes an ambiguity that lives in the
    tool list. Approximated here by the overlap of the two first sentences'
    significant words, which is crude and still catches the pairs that matter.

    **The dry-run rule.** A mutating tool with a ``preview_`` sibling must
    have that sibling registered as a separate read. A ``dry_run`` flag on the
    mutating tool leaves the call registered as a write and authorized as one.
    """
    problems: list[str] = []
    names = {s.name for s in specs}
    for spec in specs:
        if not spec.writes:
            continue
        preview = f"preview_{spec.name.split('_', 1)[-1]}"
        if preview in names and specs_by_name(specs, preview).writes:
            problems.append(f"{preview} is a write; a dry run must be a read")
        if "dry_run" in spec.input_schema.get("properties", {}):
            problems.append(
                f"{spec.name} takes a dry_run flag. A dry run is a separate "
                "read-only tool, not a flag on the mutating one."
            )
    for i, first in enumerate(specs):
        for second in specs[i + 1 :]:
            shared = _overlap(first, second)
            if shared:
                problems.append(
                    f"{first.name} and {second.name} open with the same "
                    f"words ({', '.join(sorted(shared))}); a reader could "
                    "pick either for the same request"
                )
    return problems


def specs_by_name(specs: list[ToolSpec], name: str) -> ToolSpec:
    """One spec out of a list, by name."""
    for spec in specs:
        if spec.name == name:
            return spec
    raise KeyError(name)


_STOPWORDS: frozenset[str] = frozenset(
    {
        "a",
        "an",
        "and",
        "any",
        "as",
        "at",
        "by",
        "for",
        "from",
        "in",
        "of",
        "on",
        "one",
        "or",
        "return",
        "the",
        "this",
        "to",
        "with",
        "you",
        "your",
        "it",
        "not",
        "no",
        "does",
        "use",
        "when",
        "returns",
        "will",
        "would",
        "that",
        "has",
        "have",
        "up",
        "over",
        "into",
        "before",
        "after",
        "each",
        "than",
        "them",
        "they",
        "what",
        "which",
        "while",
    }
)


#: Shared words needed before two descriptions count as overlapping, and the
#: share of the smaller sentence they have to cover. One noun in common is
#: normal -- every tool in a returns library mentions an order. Most of a
#: sentence in common is the ambiguity the model actually trips on.
OVERLAP_MIN_WORDS = 3
OVERLAP_MIN_SHARE = 0.6


def _overlap(a: ToolSpec, b: ToolSpec) -> set[str]:
    """Significant words shared by two descriptions' opening sentences.

    Returns the shared set only when it is large enough to be an ambiguity
    rather than a shared domain noun. Crude, and it still catches the pairs
    that matter, which is the trade a build check gets to make.
    """
    first = _significant(a.description)
    second = _significant(b.description)
    shared = first & second
    smaller = min(len(first), len(second)) or 1
    if len(shared) < OVERLAP_MIN_WORDS:
        return set()
    if len(shared) / smaller < OVERLAP_MIN_SHARE:
        return set()
    return shared


def _significant(description: str) -> set[str]:
    """The opening sentence's content words."""
    sentence = description.split(".")[0].lower()
    words = re.findall(r"[a-z_]{3,}", sentence)
    return {w for w in words if w not in _STOPWORDS}


class ConformingRegistry(ToolRegistry):
    """A registry that will not hold a tool which fails :func:`check`.

    Subclasses :class:`~northstar_runtime.registry.ToolRegistry` rather than
    wrapping it, so the loop, the policy engine, and the graders all see an
    ordinary registry and the conformance gate is exercised by the real
    dispatch path rather than by a fixture. The shared package is not
    modified: the gate belongs to this chapter's library, not to the runtime.
    """

    def register(
        self,
        spec: ToolSpec,
        fn: Callable[..., Any],
    ) -> ConformingRegistry:
        """Check, then register.

        Raises:
            ConformanceError: With every problem listed.
        """
        problems = check(spec, fn)
        if problems:
            listed = "\n  - ".join(problems)
            raise ConformanceError(
                f"{spec.name} cannot be registered:\n  - {listed}"
            )
        super().register(spec, fn)
        return self

    def dispatch(
        self,
        call: ToolCall,
        run_id: str | None = None,
        step: int | None = None,
    ) -> ToolResult:
        """Stamp the derived idempotency key *before* validating the call.

        The runtime's own order is validate, then stamp, which is right when
        the key is optional -- Chapter 1 needs a refund that can be made
        without one. Every write in this library declares the key *required*,
        because a call without one must not reach the tool at all, and a model
        should not be inventing keys. So the stamp happens first, derived from
        ``(run_id, step, call_id)``, and validation then sees a complete call.

        Args:
            call: The call the model asked for.
            run_id: The run. Without it there is nothing to derive from and
                the call fails validation, which is the correct outcome.
            step: The step.

        Returns:
            The observation, exactly as the parent produces it.
        """
        spec = self.spec_for(call.name)
        if self._needs_stamp(spec, call) and run_id is not None:
            call = replace(
                call,
                arguments={
                    **call.arguments,
                    "idempotency_key": idempotency_key(
                        run_id, f"{step}:{call.id}"
                    ),
                },
            )
        return super().dispatch(call, run_id, step)

    def _needs_stamp(self, spec: ToolSpec | None, call: ToolCall) -> bool:
        """Whether this call should be stamped before validation."""
        if spec is None or not self.inject_idempotency_key or not spec.writes:
            return False
        if "idempotency_key" not in spec.input_schema.get("properties", {}):
            return False
        return not call.arguments.get("idempotency_key")

    def check_library(self) -> list[str]:
        """Run the between-tools rules over everything registered so far."""
        return check_library(self.specs())

    def report(self) -> list[tuple[str, list[str]]]:
        """Per-tool conformance, for printing. Every list should be empty."""
        return [
            (spec.name, check(spec, fn)) for spec, fn in self.bindings()
        ]
