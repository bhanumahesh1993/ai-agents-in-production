"""Span attributes, and the mapping layer that keeps a rename cheap.

The OpenTelemetry GenAI semantic conventions are at development stability,
which has a precise meaning: attribute names, span names, and metric names may
change, and those changes are not required to wait for a major version bump.
Three practices contain that, and all three are here.

**Pin the convention version and record it.** :data:`SEMCONV_VERSION` goes on
every span as a resource attribute. A trace that says which vocabulary it was
written in is a trace a future query can translate; without it you are guessing
from timestamps.

**Emit through a mapping layer, not from call sites.** Agent code sets
semantic fields on an internal event object; :func:`to_conventions` converts
that object into whatever the current conventions call those fields. When a
name changes you edit one dict, not two hundred call sites.

**Namespace your own attributes.** The seven attributes that make a trace
usable during an incident mostly have no standardised name yet, so they live
under ``northstar.*``. A namespace you own can never collide with a convention
rename, and the boundary between "standard" and "ours" stays legible to whoever
reads the spans in a year.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from northstar_contracts import Money, ToolCall, ToolSpec, short_hash
from northstar_policy import Principal

__all__ = [
    "CONVENTION",
    "REQUIRED_ATTRIBUTES",
    "SEMCONV_VERSION",
    "SPAN_NAMES",
    "RunContext",
    "digest",
    "handoff_span_attributes",
    "missing_required",
    "model_span_attributes",
    "run_span_attributes",
    "to_conventions",
    "tool_span_attributes",
]

#: The convention version this instrumentation targets. Recorded on every
#: span. Check it against the current specification before building an alert
#: on any ``gen_ai.*`` name below; see ``VERSIONS.md``.
SEMCONV_VERSION = "gen-ai/1.38.0-dev"

#: Internal field name to the attribute key it is emitted as. This is the
#: whole mapping layer: a convention rename is an edit here.
CONVENTION: dict[str, str] = {
    "operation": "gen_ai.operation.name",
    "request_model": "gen_ai.request.model",
    "response_model": "gen_ai.response.model",
    "finish_reason": "gen_ai.response.finish_reasons",
    "input_tokens": "gen_ai.usage.input_tokens",
    "output_tokens": "gen_ai.usage.output_tokens",
    "cached_input_tokens": "gen_ai.usage.cached_input_tokens",
    "tool_name": "gen_ai.tool.name",
    "tool_call_id": "gen_ai.tool.call.id",
    "agent_name": "gen_ai.agent.name",
    "agent_id": "gen_ai.agent.id",
}

#: The five levels of an agent trace. The session is *not* one of them: it is
#: a correlation identifier carried on every span, because a trace root that
#: spans a week of conversation produces tens of thousands of spans that no
#: backend renders and no engineer reads.
SPAN_NAMES = {
    "run": "gen_ai.agent",
    "agent": "gen_ai.agent.invoke",
    "model": "gen_ai.model",
    "tool": "gen_ai.tool",
    "handoff": "gen_ai.handoff",
}

#: The seven attributes without which a trace is close to worthless during an
#: incident, because the questions being asked are about identity, authority,
#: and consequence rather than about duration.
REQUIRED_ATTRIBUTES: tuple[str, ...] = (
    "northstar.goal.hash",
    "northstar.agent.version",
    "northstar.config.hash",
    "northstar.principal.user",
    "northstar.budget.remaining_cents",
    "northstar.tool.args_digest",
    "northstar.side_effect.id",
)


def digest(value: Any) -> str:
    """A stable digest of a value, for grouping without reading.

    This is what lets you cluster identical calls, detect the repeated-step
    mode Chapter 16 catalogs, and confirm that an approved call is the call
    that executed, all without exporting the arguments themselves.
    """
    return short_hash(value, 16)


@dataclass
class RunContext:
    """Everything a span needs to know that the call itself does not carry.

    Attributes:
        run_id: The trace root. One goal, one budget, one termination.
        session_id: The conversation a person experiences as continuous.
            Carried on every span as a correlation id, never as a root.
        trace_id: The identifier the whole logical run shares. A child
            agent that starts a fresh one is, for attribution purposes,
            uninstrumented.
        parent_run_id: Set on a child agent's context by the handoff. This
            single field is the difference between spend with an owner and
            spend without one.
        agent_version: The agent as a deployed artifact -- prompts, tool
            set, policy, and graph together. Not the model version.
        config_hash: The effective configuration actually used, hashed. Two
            runs on the same declared version can still differ.
        principal: Who the run acted for, as, and under.
        budget_remaining: Cents left at the moment of the span. The
            attribute teams skip and then wish for, because it converts a
            flat list of spans into a story with pressure in it.
        specs: Tool contracts by name, for the version on the tool span.
        goal: The objective, hashed rather than exported.
    """

    run_id: str
    session_id: str
    agent_version: str
    config_hash: str
    principal: Principal
    budget_remaining: Money
    goal: str = ""
    trace_id: str = ""
    parent_run_id: str | None = None
    specs: dict[str, ToolSpec] = field(default_factory=dict)
    idempotency_key: str = ""

    def __post_init__(self) -> None:
        if not self.trace_id:
            self.trace_id = self.run_id

    def spec_for(self, call: ToolCall) -> ToolSpec | None:
        """The contract for a call, if the registry declared one."""
        return self.specs.get(call.name)

    def child(self, run_id: str, agent_version: str) -> RunContext:
        """A context for a subagent, with the trace carried across.

        In-process this happens for free because the context is ambient.
        Across a service boundary, a queue, or a durable-execution journal
        it happens only if somebody serialises it and restores it, and the
        resume is the case teams forget because it may land on a different
        host an hour later.
        """
        return RunContext(
            run_id=run_id,
            session_id=self.session_id,
            agent_version=agent_version,
            config_hash=self.config_hash,
            principal=self.principal,
            budget_remaining=self.budget_remaining,
            goal=self.goal,
            trace_id=self.trace_id,
            parent_run_id=self.run_id,
            specs=dict(self.specs),
        )

    def orphan(self, run_id: str, agent_version: str) -> RunContext:
        """A subagent context with the propagation deliberately broken.

        Reproduces Northstar's April failure: the child starts a fresh
        trace, so to the backend an escalated run is two unrelated traces
        with no edge between them and the expensive half has no owner.
        """
        return RunContext(
            run_id=run_id,
            session_id=self.session_id,
            agent_version=agent_version,
            config_hash=self.config_hash,
            principal=self.principal,
            budget_remaining=self.budget_remaining,
            goal=self.goal,
            trace_id=run_id,          # a new trace: the defect
            parent_run_id=None,
            specs=dict(self.specs),
        )


def _common(ctx: RunContext) -> dict[str, Any]:
    """Attributes every span in a run carries."""
    return {
        "northstar.semconv.version": SEMCONV_VERSION,
        "northstar.session.id": ctx.session_id,
        "northstar.trace.id": ctx.trace_id,
        "northstar.run.id": ctx.run_id,
        "northstar.parent_run.id": ctx.parent_run_id or "",
        "northstar.goal.hash": digest(ctx.goal),
        "northstar.agent.version": ctx.agent_version,
        "northstar.config.hash": ctx.config_hash,
        "northstar.principal.user": ctx.principal.user_id or "",
        "northstar.principal.agent": ctx.principal.agent_id,
        "northstar.principal.operator": ctx.principal.operator_id,
        "northstar.budget.remaining_cents": ctx.budget_remaining,
    }


def to_conventions(fields: dict[str, Any]) -> dict[str, Any]:
    """Translate internal field names into the current convention names.

    Fields with no mapping pass through unchanged, which is what lets the
    ``northstar.*`` namespace and the standardised namespace share one dict
    without either one having to know about the other.
    """
    return {CONVENTION.get(key, key): value for key, value in fields.items()}


def run_span_attributes(ctx: RunContext, status: str) -> dict[str, Any]:
    """The trace root: one goal, one budget, one termination."""
    return to_conventions({
        **_common(ctx),
        "operation": "invoke_agent",
        "agent_id": ctx.principal.agent_id,
        "agent_name": ctx.agent_version,
        "northstar.status": status,
    })


def model_span_attributes(
    ctx: RunContext,
    model: str,
    input_tokens: int,
    cached_input_tokens: int,
    output_tokens: int,
    finish_reason: str,
) -> dict[str, Any]:
    """One model turn, with the cached-token split carried separately.

    Cached input tokens are billed on their own terms, and an agent loop is
    the workload most affected: every turn re-sends an accumulating history
    that is mostly identical to the previous turn's. A ledger that
    multiplies total input tokens by a single rate is wrong in the
    direction of overstating cost.
    """
    return to_conventions({
        **_common(ctx),
        "operation": "chat",
        "request_model": model,
        "response_model": model,
        "finish_reason": finish_reason,
        "input_tokens": input_tokens - cached_input_tokens,
        "cached_input_tokens": cached_input_tokens,
        "output_tokens": output_tokens,
    })


def tool_span_attributes(call: ToolCall, ctx: RunContext) -> dict[str, Any]:
    """One tool execution, with the seven incident attributes on it."""
    spec = ctx.spec_for(call)
    version = spec.version if spec is not None else "unknown"
    writes = bool(spec.writes) if spec is not None else False
    return to_conventions({
        **_common(ctx),
        "operation": "execute_tool",
        "tool_name": f"{call.name}@{version}",
        "tool_call_id": call.id,
        # Ours, so a convention rename cannot collide with it.
        "northstar.tool.args_digest": digest(call.arguments),
        "northstar.tool.writes": writes,
        "northstar.idempotency_key": ctx.idempotency_key,
        # Set when the result returns, because until then it does not
        # exist. On a refund this is the receipt id from the refund
        # service, and it is the field that joins a span to the ledger row
        # it claims to have created.
        "northstar.side_effect.id": "",
    })


def handoff_span_attributes(
    ctx: RunContext,
    child: RunContext,
    reason: str,
    budget_handed: Money,
) -> dict[str, Any]:
    """A delegation, visible as a delegation.

    Without this span all you see is a gap in the parent's timeline and a
    second agent that appeared from nowhere, and you cannot tell a
    delegation from a retry.
    """
    return to_conventions({
        **_common(ctx),
        "operation": "handoff",
        "northstar.handoff.reason": reason,
        "northstar.handoff.child_run_id": child.run_id,
        "northstar.handoff.child_trace_id": child.trace_id,
        "northstar.handoff.budget_cents": budget_handed,
        "northstar.handoff.propagated": child.trace_id == ctx.trace_id,
    })


def missing_required(attributes: dict[str, Any]) -> list[str]:
    """Which of the seven required attributes a span left out.

    Empty strings count as missing for identity fields and as present for
    ``side_effect.id``, which is legitimately empty on a read.
    """
    missing: list[str] = []
    for key in REQUIRED_ATTRIBUTES:
        if key not in attributes:
            missing.append(key)
            continue
        value = attributes[key]
        if key == "northstar.side_effect.id":
            continue
        if value == "" or value is None:
            missing.append(key)
    return missing
