"""The agent runtime: providers, registry, loop, checkpointers, durability.

Everything here is provider-agnostic and framework-free on purpose. The
loop is small enough to read in one sitting, and the point of the book is
that you should be able to read it — whichever framework you end up
shipping, the concerns in this package are the ones you will still own.

::

    from northstar_runtime import AgentLoop, FakeModel, ToolRegistry
"""

from __future__ import annotations

from .checkpoint import Checkpointer, MemoryCheckpointer, SqliteCheckpointer
from .durable import (
    JOURNAL_TYPES,
    DurableRunner,
    FileJournal,
    Journal,
    JournalExhausted,
    MemoryJournal,
    ReplayDivergence,
    SimulatedCrash,
    journal_record,
)
from .loop import (
    DEFAULT_SYSTEM_PROMPT,
    AgentLoop,
    AgentLoopError,
    PolicyDenied,
    RunCancelled,
    TelemetrySink,
    default_cost_cents,
)
from .providers import (
    FakeModel,
    FlakyModel,
    LiveModel,
    LiveModelUnavailable,
    ModelProvider,
    ModelResponse,
    ScriptExhausted,
    ScriptStep,
)
from .registry import ToolRegistry, truncate_to_budget

__version__ = "1.0.0"

__all__ = [
    "DEFAULT_SYSTEM_PROMPT",
    "JOURNAL_TYPES",
    "AgentLoop",
    "AgentLoopError",
    "Checkpointer",
    "DurableRunner",
    "FakeModel",
    "FileJournal",
    "FlakyModel",
    "Journal",
    "JournalExhausted",
    "LiveModel",
    "LiveModelUnavailable",
    "MemoryCheckpointer",
    "MemoryJournal",
    "ModelProvider",
    "ModelResponse",
    "PolicyDenied",
    "ReplayDivergence",
    "RunCancelled",
    "ScriptExhausted",
    "ScriptStep",
    "SimulatedCrash",
    "SqliteCheckpointer",
    "TelemetrySink",
    "ToolRegistry",
    "__version__",
    "default_cost_cents",
    "journal_record",
    "truncate_to_budget",
]
