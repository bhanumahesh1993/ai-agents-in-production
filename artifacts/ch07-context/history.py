"""``get_run_history``: the retrieval path that makes compaction paging.

A summary block carries the step range it replaced. Without a way to read
that range back, the range is decoration and the agent has simply
forgotten. With one, compaction is paging: the detail is still there, it
is just no longer resident, and fetching it costs a turn rather than a
mistake.

The tool reads the run's own event log, which is append-only and
authoritative. It is a read tool, it is naturally idempotent, and it is
budgeted like any other, because a history tool that returns the whole log
has reintroduced the problem compaction was solving.
"""

from __future__ import annotations

from typing import Any

from northstar_contracts import EventLog, ToolSpec

__all__ = ["GET_RUN_HISTORY", "make_get_run_history"]

GET_RUN_HISTORY = ToolSpec(
    name="get_run_history",
    description=(
        "Read this run's own event log for a step range that a summary "
        "replaced. Use this when a summary block references steps you "
        "need detail from. Returns one record per event with its step, "
        "type, tool name, and whether the call succeeded. Amounts are "
        "integer cents. Does not return other runs, and does not change "
        "anything."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "from_step": {"type": "integer"},
            "to_step": {"type": "integer"},
        },
        "required": ["from_step", "to_step"],
        "additionalProperties": False,
    },
    output_schema={
        "type": "object",
        "properties": {
            "records": {"type": "array"},
            "from_step": {"type": "integer"},
            "to_step": {"type": "integer"},
            "total": {"type": "integer"},
        },
        "required": ["records", "from_step", "to_step", "total"],
        "additionalProperties": False,
    },
    writes=False,
    idempotent=True,
    max_result_tokens=600,
)


def make_get_run_history(
    events: EventLog,
) -> tuple[ToolSpec, Any]:
    """Bind the history tool to one run's event log.

    Returns:
        A ``(spec, fn)`` pair ready for ``ToolRegistry.register``.
    """

    def get_run_history(from_step: int, to_step: int) -> dict[str, Any]:
        """Return the event records covering ``[from_step, to_step]``."""
        records = [
            {
                "step": r["step"],
                "type": r["type"],
                "tool": r["payload"].get("tool"),
                "ok": r["payload"].get("ok"),
                "arguments": r["payload"].get("arguments"),
            }
            for r in events.records
            if from_step <= r["step"] <= to_step
            and r["type"] in ("tool.called", "tool.result")
        ]
        return {
            "records": records,
            "from_step": from_step,
            "to_step": to_step,
            "total": len(records),
        }

    return GET_RUN_HISTORY, get_run_history
