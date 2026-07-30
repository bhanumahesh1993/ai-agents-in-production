"""One call to instrument a loop, and the backend as one environment variable.

Telemetry that requires edits at every call site is telemetry that decays, so
attaching it is a single call and the agent loop knows nothing about it. The
loop emits events; this subscribes.

The exporter is chosen by ``NORTHSTAR_OTEL_EXPORTER`` and defaults to
``console``, which needs nothing installed. ``otlp`` reads the standard
OpenTelemetry variables (``OTEL_EXPORTER_OTLP_ENDPOINT``, ``_HEADERS``), so the
backend is deployment configuration rather than application code. That choice
is not re-implemented here: :func:`northstar_telemetry.instrument` already
decides whether OpenTelemetry is importable and raises a clear error naming the
install command when it is not, so this module calls it and then upgrades the
sink it attached.

What the upgrade adds is the part the chapter is about. The package's
instrumentation answers whether a model call succeeded. This one answers which
decision, taken by which version of which agent, on whose behalf, under what
remaining budget, spent this money and moved which specific dollars -- which
means the seven attributes from :mod:`spans`, the cached-token split from
:mod:`cost`, and a count of what is missing, because trace completeness is a
service level indicator rather than a hope.

The side-effect identifier is the awkward one, and honestly so. It does not
exist until the tool returns, and the loop's event stream carries the call's
outcome without carrying the receipt. :class:`TracedTools` captures it at the
action boundary -- the only place it is available -- and the instrumentation
reads it when it closes the tool span. Without that, every write span in the
trace has a hole exactly where the join key to the ledger belongs.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any

from cost import CostLedger, cached_split
from northstar_contracts import Money, ToolCall, ToolResult, ToolSpec, World
from northstar_policy import Principal
from northstar_runtime import AgentLoop, ToolRegistry
from northstar_telemetry import Instrumentation, Span
from northstar_telemetry import instrument as attach_exporter
from redaction import REDACTOR
from spans import (
    REQUIRED_ATTRIBUTES,
    SPAN_NAMES,
    RunContext,
    handoff_span_attributes,
    missing_required,
    model_span_attributes,
    run_span_attributes,
    tool_span_attributes,
)

__all__ = [
    "AGENT_VERSION",
    "BUDGET_CENTS",
    "CONFIG_HASH",
    "EXPORTER_ENV",
    "SESSION_ID",
    "SPECIALIST_VERSION",
    "NorthstarInstrumentation",
    "SideEffectIndex",
    "TracedTools",
    "build_context",
    "chosen_exporter",
    "instrument",
]

#: The variable that picks the backend. One name, so a deployment can change
#: where traces go without a code change.
EXPORTER_ENV = "NORTHSTAR_OTEL_EXPORTER"

#: The agent as a deployed artifact: prompts, tool set, policy, and graph
#: together. Not the model version.
AGENT_VERSION = "support-agent@v9"
SPECIALIST_VERSION = "fraud-review-agent@v4"

#: The effective configuration actually used, hashed. Two runs on the same
#: declared version can differ if a flag or a tenant override moved.
CONFIG_HASH = "cfg-8a41c2d0"

#: The conversation a person experiences as one continuous thing. Carried on
#: every span as a correlation id, and never a root span: a trace spanning a
#: week of conversation produces tens of thousands of spans no backend renders.
SESSION_ID = "sess-2026-04-16-northstar"

BUDGET_CENTS: Money = 200


def chosen_exporter(default: str = "console") -> str:
    """The exporter this deployment asked for."""
    return os.environ.get(EXPORTER_ENV, default)


@dataclass
class SideEffectIndex:
    """Receipt identifiers, keyed by the call that produced them.

    The join key between a span and the ledger row it claims to have created.
    It is captured at the action boundary because that is the only place it
    exists: the loop's ``tool.result`` event says whether the call succeeded,
    not what came back.
    """

    #: ``(run_id, call_id)`` to the receipt and the key that was presented.
    entries: dict[tuple[str, str], dict[str, Any]] = field(
        default_factory=dict
    )

    #: Receipt fields, in the order they are looked for. Every Northstar
    #: write returns exactly one of them.
    RECEIPT_FIELDS = ("refund_id", "message_id", "case_id", "settlement_id")

    def record(
        self,
        run_id: str,
        call: ToolCall,
        result: ToolResult,
        arguments: dict[str, Any],
    ) -> None:
        """Capture the receipt and the idempotency key for one call."""
        content = result.content if isinstance(result.content, dict) else {}
        receipt = ""
        for name in self.RECEIPT_FIELDS:
            if content.get(name):
                receipt = str(content[name])
                break
        self.entries[(run_id, call.id)] = {
            "side_effect_id": receipt,
            "idempotency_key": str(arguments.get("idempotency_key") or ""),
            "duplicate": bool(content.get("duplicate", False)),
        }

    def for_call(self, run_id: str, call_id: str) -> dict[str, Any]:
        """What was captured for one call, or empty defaults."""
        return self.entries.get(
            (run_id, call_id),
            {"side_effect_id": "", "idempotency_key": "", "duplicate": False},
        )


class TracedTools(ToolRegistry):
    """A registry that captures what a span needs and changes nothing else.

    Instrumentation at the action boundary rather than inside the tools: the
    tools stay unaware, and the one fact the event stream cannot carry -- the
    receipt the target system returned -- is recorded where it is available.
    """

    def __init__(
        self,
        base: ToolRegistry,
        run_id: str,
        effects: SideEffectIndex,
    ) -> None:
        super().__init__(
            inject_idempotency_key=True, validate=base.validate
        )
        self.register_all(base.bindings())
        self.run_id = run_id
        self.effects = effects

    def dispatch(
        self,
        call: ToolCall,
        run_id: str | None = None,
        step: int | None = None,
    ) -> ToolResult:
        """Dispatch, then record the receipt against the call that got it."""
        result = super().dispatch(call, run_id=run_id, step=step)
        spec = self.spec_for(call.name)
        if spec is not None and spec.writes:
            arguments = dict(call.arguments)
            arguments.setdefault(
                "idempotency_key",
                _stamped_key(self, call, run_id or self.run_id, step),
            )
            self.effects.record(
                run_id or self.run_id, call, result, arguments
            )
        return result


def _stamped_key(
    registry: ToolRegistry,
    call: ToolCall,
    run_id: str,
    step: int | None,
) -> str:
    """Recompute the key the registry stamped, so the span can carry it.

    Derived, not stored, which is the same argument Chapter 8 makes: any
    process holding the run and step recomputes it.
    """
    from northstar_contracts import idempotency_key

    if call.arguments.get("idempotency_key"):
        return str(call.arguments["idempotency_key"])
    return idempotency_key(run_id, f"{step}:{call.id}")


def build_context(
    run_id: str,
    goal: str,
    *,
    principal: Principal,
    specs: list[ToolSpec] | None = None,
    agent_version: str = AGENT_VERSION,
    session_id: str = SESSION_ID,
    budget_cents: Money = BUDGET_CENTS,
) -> RunContext:
    """Everything a span needs that the call itself does not carry."""
    contracts = specs if specs is not None else World().tool_specs()
    return RunContext(
        run_id=run_id,
        session_id=session_id,
        agent_version=agent_version,
        config_hash=CONFIG_HASH,
        principal=principal,
        budget_remaining=budget_cents,
        goal=goal,
        specs={spec.name: spec for spec in contracts},
    )


class NorthstarInstrumentation(Instrumentation):
    """The event stream, as spans that answer an incident's questions.

    Args:
        exporter: Anything with ``export(span)``. Chosen by
            :func:`instrument` through the package, so the with- and
            without-OpenTelemetry cases are handled in one place.
        ctx: The run's context. Mutated as the run proceeds, because budget
            remaining is a property of the moment rather than of the run.
        cost: The trace-linked cost ledger. Keyed by ``(run_id, span_id)``,
            so a replayed step overwrites rather than double-counts.
        effects: The side-effect index, filled by :class:`TracedTools`.
    """

    def __init__(
        self,
        exporter: Any,
        *,
        ctx: RunContext,
        cost: CostLedger,
        effects: SideEffectIndex,
    ) -> None:
        super().__init__(
            exporter, redactor=REDACTOR, capture_arguments=False
        )
        self.ctx = ctx
        self.cost = cost
        self.effects = effects
        #: One entry per span that left out a required attribute. Trace
        #: completeness is measured from this, not asserted in a wiki.
        self.missing: list[dict[str, Any]] = []
        #: Write spans with no receipt id. A hole exactly where the join key
        #: to the ledger belongs.
        self.writes_without_receipt: list[str] = []
        self._previous_input_tokens = 0
        self._model_calls = 0
        self._tool_calls = 0

    # ------------------------------------------------------------- the spans

    def _on_run_started(
        self,
        run_id: str,
        ts: float,
        payload: dict[str, Any],
        record: dict[str, Any],
    ) -> None:
        self._open_runs[run_id] = {"start": ts, "status": "running"}

    def _on_model_called(
        self,
        run_id: str,
        ts: float,
        payload: dict[str, Any],
        record: dict[str, Any],
    ) -> None:
        total_input = int(payload.get("input_tokens", 0))
        output = int(payload.get("output_tokens", 0))
        _, cached = cached_split(total_input, self._previous_input_tokens)
        self._previous_input_tokens = total_input
        self._model_calls += 1

        self.cost.record(
            model=str(payload.get("model", "unknown")),
            input_tokens=total_input,
            cached_input_tokens=cached,
            output_tokens=output,
            run_id=run_id,
            span_id=f"model:{record.get('step')}",
            component="model",
        )
        self.ctx.budget_remaining = max(
            0, self.ctx.budget_remaining - int(payload.get("cost_cents", 0))
        )
        self._finish(
            Span(
                name=SPAN_NAMES["model"],
                attributes=self._attrs(
                    model_span_attributes(
                        self.ctx,
                        model=str(payload.get("model", "unknown")),
                        input_tokens=total_input,
                        cached_input_tokens=cached,
                        output_tokens=output,
                        finish_reason=str(payload.get("stop_reason", "")),
                    )
                ),
                start_time=self._last_ts.get(run_id, ts),
                end_time=ts,
                parent=SPAN_NAMES["run"],
            )
        )

    def _on_tool_called(
        self,
        run_id: str,
        ts: float,
        payload: dict[str, Any],
        record: dict[str, Any],
    ) -> None:
        key = (run_id, str(payload.get("call_id", "")))
        self._open_tools[key] = {
            "start": ts,
            "payload": payload,
            "record": record,
        }
        self._tool_calls += 1

    def _on_tool_result(
        self,
        run_id: str,
        ts: float,
        payload: dict[str, Any],
        record: dict[str, Any],
    ) -> None:
        call_id = str(payload.get("call_id", ""))
        started = self._open_tools.pop((run_id, call_id), None)
        arguments = dict(
            (started or {}).get("payload", {}).get("arguments") or {}
        )
        call = ToolCall(
            id=call_id, name=str(payload.get("tool", "")), arguments=arguments
        )
        captured = self.effects.for_call(run_id, call_id)
        self.ctx.idempotency_key = captured["idempotency_key"]

        attributes = tool_span_attributes(call, self.ctx)
        attributes["northstar.side_effect.id"] = captured["side_effect_id"]
        attributes["northstar.side_effect.duplicate"] = captured["duplicate"]
        attributes["northstar.ok"] = bool(payload.get("ok"))
        attributes["northstar.attempt"] = int(payload.get("attempt", 1))
        # The three-bucket policy, applied to the one attribute that carries
        # anything a customer wrote. Dropped in-process, before the exporter.
        attributes["northstar.tool.arguments"] = REDACTOR.apply(
            {"arguments": arguments}
        )["arguments"]

        writes = bool(attributes.get("northstar.tool.writes"))
        if writes and not captured["side_effect_id"]:
            self.writes_without_receipt.append(f"{run_id}:{call.name}")

        self._finish(
            Span(
                name=SPAN_NAMES["tool"],
                attributes=self._attrs(attributes),
                start_time=started["start"] if started else ts,
                end_time=ts,
                status="ok" if payload.get("ok") else "error",
                parent=SPAN_NAMES["run"],
            )
        )

    def _on_run_finished(
        self,
        run_id: str,
        ts: float,
        payload: dict[str, Any],
        record: dict[str, Any],
    ) -> None:
        run = self._open_runs.pop(run_id, None)
        status = str(payload.get("status", "unknown"))
        attributes = run_span_attributes(self.ctx, status)
        attributes["northstar.model_calls"] = self._model_calls
        attributes["northstar.tool_calls"] = self._tool_calls
        attributes["northstar.cost_cents"] = self.cost.per_run_cents(run_id)
        # A run root has no side effect of its own. Empty is legitimate here
        # and the completeness check knows it.
        attributes["northstar.side_effect.id"] = ""
        self._finish(
            Span(
                name=SPAN_NAMES["run"],
                attributes=self._attrs(attributes),
                start_time=(run or {}).get("start", ts),
                end_time=ts,
                status="ok" if status == "succeeded" else "error",
            )
        )

    def record_handoff(
        self,
        child: RunContext,
        reason: str,
        budget_handed: Money,
        at: float = 0.0,
    ) -> Span:
        """Emit the handoff span, so a delegation looks like a delegation.

        Without it all you see is a gap in the parent's timeline and a second
        agent that appeared from nowhere, and you cannot tell a delegation
        from a retry.
        """
        attributes = handoff_span_attributes(
            self.ctx, child, reason, budget_handed
        )
        attributes["northstar.side_effect.id"] = ""
        span = Span(
            name=SPAN_NAMES["handoff"],
            attributes=self._attrs(attributes),
            start_time=at,
            end_time=at,
            parent=SPAN_NAMES["run"],
        )
        self._finish(span)
        return span

    # -------------------------------------------------------------- measured

    def _finish(self, span: Span) -> None:
        """Record, check, and export one finished span.

        The check is the point. Requiring the seven attributes turns
        instrumentation into something you can measure rather than hope for,
        and a missing-trace rate drifting upward is a silent loss of the
        ability to investigate anything.
        """
        gaps = missing_required(span.attributes, span.name)
        if gaps:
            self.missing.append({"span": span.name, "missing": gaps})
        super()._finish(span)

    def trace_ids(self) -> set[str]:
        """Every distinct trace id in this instrumentation's spans."""
        return {
            str(s.attributes.get("northstar.trace.id", ""))
            for s in self.spans
        }

    def summary(self) -> dict[str, Any]:
        """Counts and cost, in the shape a per-run tile needs."""
        return {
            "spans": len(self.spans),
            "run_spans": len(self.named(SPAN_NAMES["run"])),
            "model_spans": len(self.named(SPAN_NAMES["model"])),
            "tool_spans": len(self.named(SPAN_NAMES["tool"])),
            "handoff_spans": len(self.named(SPAN_NAMES["handoff"])),
            "errors": sum(1 for s in self.spans if s.status == "error"),
            "required_attributes": len(REQUIRED_ATTRIBUTES),
            "spans_missing_attributes": len(self.missing),
            "writes_without_receipt": len(self.writes_without_receipt),
            "cost_cents": self.cost.per_run_cents(self.ctx.run_id),
        }


def instrument(
    loop: AgentLoop,
    ctx: RunContext,
    exporter: str | None = None,
    *,
    cost: CostLedger | None = None,
    effects: SideEffectIndex | None = None,
    stream: Any | None = None,
) -> NorthstarInstrumentation:
    """Attach tracing to one loop. This is the whole wiring.

    Args:
        loop: The loop to instrument. Its ``telemetry`` attribute is set.
        ctx: The run's context, carrying the identity and version attributes
            no convention has a name for yet.
        exporter: Overrides ``NORTHSTAR_OTEL_EXPORTER``. One of ``console``,
            ``memory``, ``none``, ``otel``, ``otlp``, or ``auto``.
        cost: Ledger to accumulate into. Share one across a run tree and the
            child's spend rolls up to the parent.
        effects: Side-effect index, shared with the run's
            :class:`TracedTools`.
        stream: Output stream for the console exporter.

    Returns:
        The instrumentation, so tests and dashboards can read the spans back.
    """
    chosen = exporter if exporter is not None else chosen_exporter()
    # The package owns the exporter decision, including the case where
    # OpenTelemetry is not installed. Calling it here rather than
    # reimplementing the branch is the whole reason the shim exists.
    baseline = attach_exporter(
        loop, exporter=chosen, redactor=REDACTOR, stream=stream
    )
    telemetry = NorthstarInstrumentation(
        baseline.exporter,
        ctx=ctx,
        cost=cost if cost is not None else CostLedger(),
        effects=effects if effects is not None else SideEffectIndex(),
    )
    # Replaces the sink the line above attached. The baseline never sees an
    # event; what it contributed was the backend and its error message.
    loop.telemetry = telemetry
    return telemetry
