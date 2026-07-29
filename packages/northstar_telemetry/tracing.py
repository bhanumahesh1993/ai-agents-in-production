"""OpenTelemetry instrumentation for agent runs.

An agent trace has to answer questions a request trace never has to: what
was the agent trying to do, what did it decide, which tool did it call with
which arguments, did that tool change anything, and what did the whole
thing cost. The OpenTelemetry GenAI semantic conventions give those fields
names, and using them is how you avoid re-plumbing every dashboard when you
change observability vendors.

Two caveats stated plainly, because the book states them plainly:

* The GenAI conventions were still moving as of July 2026. Attribute names
  here follow the ``gen_ai.*`` shape; re-check them against the current
  specification before you build alerts on them. See ``VERSIONS.md``.
* This module imports ``opentelemetry`` lazily and only if you ask for it.
  Importing ``northstar_telemetry`` on a machine with no OpenTelemetry
  installed works, emits to a console or in-memory shim, and never fails.
"""

from __future__ import annotations

import importlib.util
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, TextIO

from .cost import CostLedger
from .redaction import Redactor

if TYPE_CHECKING:  # pragma: no cover - typing only
    from northstar_runtime import AgentLoop

__all__ = [
    "GEN_AI_SPANS",
    "Instrumentation",
    "Span",
    "SpanRecorder",
    "TelemetryUnavailable",
    "instrument",
]

#: The three span names the book uses, and what each one covers.
GEN_AI_SPANS = {
    "gen_ai.agent": "one whole run, from goal to terminal state",
    "gen_ai.model": "one model call",
    "gen_ai.tool": "one tool execution",
}


class TelemetryUnavailable(RuntimeError):
    """An exporter was requested that this machine cannot provide."""


@dataclass
class Span:
    """One finished span, in the shim's own representation.

    Kept deliberately simple and vendor-free: a name, attributes, a start
    and end, and a status. Everything the book asserts about traces can be
    asserted against this, offline, in a unit test — which is the point.
    """

    name: str
    attributes: dict[str, Any] = field(default_factory=dict)
    start_time: float = 0.0
    end_time: float = 0.0
    status: str = "ok"
    parent: str | None = None

    @property
    def duration_seconds(self) -> float:
        """How long the span lasted."""
        return max(0.0, self.end_time - self.start_time)

    def to_dict(self) -> dict[str, Any]:
        """JSON-serialisable form."""
        return {
            "name": self.name,
            "attributes": dict(self.attributes),
            "start_time": self.start_time,
            "end_time": self.end_time,
            "duration_seconds": round(self.duration_seconds, 6),
            "status": self.status,
            "parent": self.parent,
        }


class SpanRecorder:
    """Collects finished spans in memory. The test-friendly exporter."""

    def __init__(self) -> None:
        self.spans: list[Span] = []

    def export(self, span: Span) -> None:
        """Record one finished span."""
        self.spans.append(span)

    def named(self, name: str) -> list[Span]:
        """Every recorded span with a given name."""
        return [s for s in self.spans if s.name == name]

    def clear(self) -> None:
        """Drop everything recorded so far."""
        self.spans.clear()


class _ConsoleExporter:
    """Prints one line per span. Enough to debug a run in a terminal."""

    def __init__(self, stream: TextIO | None = None) -> None:
        self.stream = stream or sys.stdout

    def export(self, span: Span) -> None:
        """Write one span as a single readable line."""
        attrs = " ".join(
            f"{k}={v}"
            for k, v in sorted(span.attributes.items())
            if k != "gen_ai.prompt"
        )
        self.stream.write(
            f"[{span.name}] {span.duration_seconds * 1000:.1f}ms "
            f"{span.status} {attrs}\n"
        )


class _NullExporter:
    """Drops every span."""

    def export(self, span: Span) -> None:
        """Do nothing."""


class _OtelExporter:
    """Forwards shim spans to a real OpenTelemetry tracer.

    Constructed only when the caller asks for OpenTelemetry *and* the
    packages are importable. The import happens here, inside ``__init__``,
    so that neither importing this module nor running the test suite
    requires OpenTelemetry to be installed.
    """

    def __init__(self, service_name: str = "northstar-agent") -> None:
        if importlib.util.find_spec("opentelemetry") is None:
            raise TelemetryUnavailable(
                "opentelemetry is not installed. "
                'Run: pip install -e ".[otel]" '
                "or use exporter='console' for the built-in shim."
            )
        from opentelemetry import trace  # noqa: PLC0415

        self._tracer = trace.get_tracer(service_name)

    def export(self, span: Span) -> None:
        """Emit the span through the OpenTelemetry API."""
        otel_span = self._tracer.start_span(
            span.name,
            start_time=int(span.start_time * 1_000_000_000),
            attributes={
                k: v
                for k, v in span.attributes.items()
                if isinstance(v, str | int | float | bool)
            },
        )
        otel_span.end(end_time=int(span.end_time * 1_000_000_000))


Exporter = Callable[[Span], None]


class Instrumentation:
    """Turns an agent's event stream into ``gen_ai.*`` spans.

    The agent loop knows nothing about this class. It emits events; this
    subscribes. That separation is deliberate: telemetry that is woven
    through the loop is telemetry you cannot turn off, cannot test, and
    cannot swap.

    Args:
        exporter: Object with an ``export(span)`` method.
        redactor: Applied to every attribute value before export. Traces
            leave your trust boundary; tool arguments contain customer
            data. Redact at the boundary, not at the dashboard.
        ledger: Cost ledger fed from every model span.
        capture_arguments: Include tool arguments on tool spans. On by
            default because they are what make an agent trace useful, and
            redacted by default because they are also what makes one
            dangerous.
    """

    def __init__(
        self,
        exporter: Any,
        *,
        redactor: Redactor | None = None,
        ledger: CostLedger | None = None,
        capture_arguments: bool = True,
    ) -> None:
        self.exporter = exporter
        self.redactor = redactor or Redactor.default()
        self.ledger = ledger or CostLedger()
        self.capture_arguments = capture_arguments
        self.spans: list[Span] = []
        self._open_runs: dict[str, dict[str, Any]] = {}
        self._open_tools: dict[tuple[str, str], dict[str, Any]] = {}
        self._last_ts: dict[str, float] = {}

    # ------------------------------------------------------------ the sink

    def emit(self, record: dict[str, Any]) -> None:
        """Consume one event-log record. This is the ``TelemetrySink``."""
        run_id = str(record["run_id"])
        kind = str(record["type"])
        ts = float(record["ts"])
        payload: dict[str, Any] = record.get("payload") or {}

        handler = {
            "run.started": self._on_run_started,
            "model.called": self._on_model_called,
            "tool.called": self._on_tool_called,
            "tool.result": self._on_tool_result,
            "approval.requested": self._on_approval,
            "run.finished": self._on_run_finished,
        }.get(kind)
        if handler is not None:
            handler(run_id, ts, payload, record)
        self._last_ts[run_id] = ts

    # --------------------------------------------------------------- events

    def _on_run_started(
        self,
        run_id: str,
        ts: float,
        payload: dict[str, Any],
        record: dict[str, Any],
    ) -> None:
        principal = payload.get("principal") or {}
        self._open_runs[run_id] = {
            "start": ts,
            "attributes": {
                "gen_ai.operation.name": "invoke_agent",
                "gen_ai.agent.id": principal.get("agent_id", "unknown"),
                "gen_ai.agent.name": principal.get("agent_id", "unknown"),
                "northstar.run_id": run_id,
                "northstar.user_id": principal.get("user_id"),
                "northstar.tools": ",".join(payload.get("tools", [])),
            },
            "input_tokens": 0,
            "output_tokens": 0,
            "cost_cents": 0,
            "tool_calls": 0,
            "approvals": 0,
        }

    def _on_model_called(
        self,
        run_id: str,
        ts: float,
        payload: dict[str, Any],
        record: dict[str, Any],
    ) -> None:
        run = self._open_runs.get(run_id)
        model = str(payload.get("model", "unknown"))
        input_tokens = int(payload.get("input_tokens", 0))
        output_tokens = int(payload.get("output_tokens", 0))
        cost = self.ledger.record(
            model, input_tokens, output_tokens, run_id=run_id
        )
        if run is not None:
            run["input_tokens"] += input_tokens
            run["output_tokens"] += output_tokens
            run["cost_cents"] += int(payload.get("cost_cents", 0))

        self._finish(
            Span(
                name="gen_ai.model",
                attributes=self._attrs(
                    {
                        "gen_ai.operation.name": "chat",
                        "gen_ai.request.model": model,
                        "gen_ai.response.model": model,
                        "gen_ai.response.finish_reasons": payload.get(
                            "stop_reason"
                        ),
                        "gen_ai.usage.input_tokens": input_tokens,
                        "gen_ai.usage.output_tokens": output_tokens,
                        "northstar.run_id": run_id,
                        "northstar.step": record.get("step"),
                        "northstar.cost_cents": cost,
                    }
                ),
                # The loop emits this event when the call returns, so the
                # previous event is the best available start time. An
                # approximation, and labelled as one.
                start_time=self._last_ts.get(run_id, ts),
                end_time=ts,
                parent="gen_ai.agent",
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
        self._open_tools[key] = {"start": ts, "payload": payload, "record": record}
        run = self._open_runs.get(run_id)
        if run is not None:
            run["tool_calls"] += 1

    def _on_tool_result(
        self,
        run_id: str,
        ts: float,
        payload: dict[str, Any],
        record: dict[str, Any],
    ) -> None:
        key = (run_id, str(payload.get("call_id", "")))
        started = self._open_tools.pop(key, None)
        attributes: dict[str, Any] = {
            "gen_ai.operation.name": "execute_tool",
            "gen_ai.tool.name": payload.get("tool"),
            "gen_ai.tool.call.id": payload.get("call_id"),
            "northstar.run_id": run_id,
            "northstar.step": record.get("step"),
            "northstar.ok": payload.get("ok"),
            "northstar.truncated": payload.get("truncated"),
            "northstar.attempt": payload.get("attempt", 1),
            "northstar.result_tokens": payload.get("result_tokens"),
        }
        if payload.get("error"):
            attributes["northstar.error"] = payload["error"]
        if self.capture_arguments and started is not None:
            attributes["northstar.tool.arguments"] = (
                started["payload"].get("arguments")
            )
        self._finish(
            Span(
                name="gen_ai.tool",
                attributes=self._attrs(attributes),
                start_time=started["start"] if started else ts,
                end_time=ts,
                status="ok" if payload.get("ok") else "error",
                parent="gen_ai.agent",
            )
        )

    def _on_approval(
        self,
        run_id: str,
        ts: float,
        payload: dict[str, Any],
        record: dict[str, Any],
    ) -> None:
        run = self._open_runs.get(run_id)
        if run is not None:
            run["approvals"] += 1

    def _on_run_finished(
        self,
        run_id: str,
        ts: float,
        payload: dict[str, Any],
        record: dict[str, Any],
    ) -> None:
        run = self._open_runs.pop(run_id, None)
        if run is None:
            return
        status = str(payload.get("status", "unknown"))
        attributes = dict(run["attributes"])
        attributes.update(
            {
                "gen_ai.usage.input_tokens": run["input_tokens"],
                "gen_ai.usage.output_tokens": run["output_tokens"],
                "northstar.status": status,
                "northstar.steps": record.get("step"),
                "northstar.tool_calls": run["tool_calls"],
                "northstar.approvals_requested": run["approvals"],
                "northstar.cost_cents": run["cost_cents"],
            }
        )
        self._finish(
            Span(
                name="gen_ai.agent",
                attributes=self._attrs(attributes),
                start_time=run["start"],
                end_time=ts,
                status="ok" if status == "succeeded" else "error",
            )
        )

    # -------------------------------------------------------------- helpers

    def _attrs(self, attributes: dict[str, Any]) -> dict[str, Any]:
        """Drop empty values and redact what is left."""
        cleaned = {k: v for k, v in attributes.items() if v is not None}
        redacted = self.redactor.redact(cleaned)
        return redacted if isinstance(redacted, dict) else cleaned

    def _finish(self, span: Span) -> None:
        """Record and export one finished span."""
        self.spans.append(span)
        self.exporter.export(span)

    # --------------------------------------------------------------- report

    def named(self, name: str) -> list[Span]:
        """Every span emitted with a given name."""
        return [s for s in self.spans if s.name == name]

    def summary(self) -> dict[str, Any]:
        """Counts and cost, the shape a per-run dashboard tile needs."""
        return {
            "spans": len(self.spans),
            "agent_spans": len(self.named("gen_ai.agent")),
            "model_spans": len(self.named("gen_ai.model")),
            "tool_spans": len(self.named("gen_ai.tool")),
            "errors": sum(1 for s in self.spans if s.status == "error"),
            "cost_cents": self.ledger.total_cents(),
        }


def instrument(
    loop: AgentLoop,
    exporter: str = "console",
    *,
    redactor: Redactor | None = None,
    ledger: CostLedger | None = None,
    service_name: str = "northstar-agent",
    stream: TextIO | None = None,
) -> Instrumentation:
    """Attach tracing to an agent loop.

    Args:
        loop: The loop to instrument. Its ``telemetry`` attribute is set,
            which is all the wiring there is.
        exporter: One of

            ``"console"``
                One line per span on stdout. The default, because it works
                everywhere and needs nothing installed.
            ``"memory"``
                Spans kept in a list. Use this in tests.
            ``"none"``
                Spans built and dropped. Useful for measuring the overhead
                of instrumentation itself.
            ``"otel"`` or ``"otlp"``
                Real OpenTelemetry spans through the API. Requires the
                ``otel`` extra; raises :class:`TelemetryUnavailable` with
                the install command if it is missing.
            ``"auto"``
                OpenTelemetry if it is installed, console if it is not.
        redactor: Field redaction applied before export.
        ledger: Cost ledger to accumulate into.
        service_name: Tracer name, for the OpenTelemetry exporter.
        stream: Output stream for the console exporter.

    Returns:
        The :class:`Instrumentation`, so tests and dashboards can read the
        spans back. The contract in the book writes this as returning
        ``None``; returning the object is a superset and callers that
        ignore it behave identically.

    Example:
        >>> telemetry = instrument(loop, exporter="memory")
        >>> _ = loop.run("refund order NR-2026-0041903")
        >>> telemetry.summary()["agent_spans"]
        1
    """
    chosen = exporter.lower()
    if chosen == "auto":
        chosen = (
            "otel"
            if importlib.util.find_spec("opentelemetry") is not None
            else "console"
        )

    backend: Any
    if chosen == "memory":
        backend = SpanRecorder()
    elif chosen == "none":
        backend = _NullExporter()
    elif chosen in ("otel", "otlp"):
        backend = _OtelExporter(service_name)
    elif chosen == "console":
        backend = _ConsoleExporter(stream)
    else:
        known = "console, memory, none, otel, otlp, auto"
        raise ValueError(
            f"unknown exporter {exporter!r}; expected one of {known}"
        )

    instrumentation = Instrumentation(
        backend, redactor=redactor, ledger=ledger
    )
    loop.telemetry = instrumentation
    return instrumentation
