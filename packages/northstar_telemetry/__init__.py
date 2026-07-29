"""Instrumentation, cost attribution, and redaction.

Importing this package never requires OpenTelemetry. Ask for the
OpenTelemetry exporter and you get it if it is installed, and a clear error
naming the install command if it is not::

    from northstar_telemetry import CostLedger, Redactor, instrument

    telemetry = instrument(loop, exporter="memory")
"""

from __future__ import annotations

from .cost import (
    DEFAULT_PRICE,
    ILLUSTRATIVE_PRICES,
    CostEntry,
    CostLedger,
    ModelPrice,
)
from .redaction import (
    DEFAULT_PATTERNS,
    DEFAULT_SENSITIVE_FIELDS,
    Redactor,
)
from .tracing import (
    GEN_AI_SPANS,
    Instrumentation,
    Span,
    SpanRecorder,
    TelemetryUnavailable,
    instrument,
)

__version__ = "1.0.0"

__all__ = [
    "DEFAULT_PATTERNS",
    "DEFAULT_PRICE",
    "DEFAULT_SENSITIVE_FIELDS",
    "GEN_AI_SPANS",
    "ILLUSTRATIVE_PRICES",
    "CostEntry",
    "CostLedger",
    "Instrumentation",
    "ModelPrice",
    "Redactor",
    "Span",
    "SpanRecorder",
    "TelemetryUnavailable",
    "__version__",
    "instrument",
]
