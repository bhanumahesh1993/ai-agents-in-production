"""Typed contracts shared by the whole book.

Everything else in this repository — the runtime, the policy engine, the
telemetry layer, the evaluation harness, and all twenty-eight chapter
artifacts — depends on this package and on nothing else. Keep it that way:
a contracts package that imports a runtime is no longer a contract.

Import root::

    from northstar_contracts import Message, ToolCall, ToolSpec, World
"""

from __future__ import annotations

from .errors import (
    RetryableToolError,
    ToolError,
    ToolNotFound,
    ToolPermissionError,
    ToolTimeout,
    ToolValidationError,
)
from .events import EVENT_TYPES, EventLog, event_record
from .ids import canonical_json, content_hash, idempotency_key, short_hash
from .models import (
    Currency,
    Message,
    Money,
    Role,
    RunState,
    RunStatus,
    ToolCall,
    ToolResult,
    ToolSpec,
)
from .tokens import estimate_tokens
from .world import (
    FAULT_KINDS,
    REFUND_APPROVAL_THRESHOLD_CENTS,
    Fault,
    World,
)

__version__ = "1.0.0"

__all__ = [
    "EVENT_TYPES",
    "FAULT_KINDS",
    "REFUND_APPROVAL_THRESHOLD_CENTS",
    "Currency",
    "EventLog",
    "Fault",
    "Message",
    "Money",
    "RetryableToolError",
    "Role",
    "RunState",
    "RunStatus",
    "ToolCall",
    "ToolError",
    "ToolNotFound",
    "ToolPermissionError",
    "ToolResult",
    "ToolSpec",
    "ToolTimeout",
    "ToolValidationError",
    "World",
    "__version__",
    "canonical_json",
    "content_hash",
    "estimate_tokens",
    "event_record",
    "idempotency_key",
    "short_hash",
]
