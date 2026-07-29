"""Typed contracts shared by every agent, tool, and grader in the book.

Three rules govern everything in this module.

1. Money is an integer number of cents. Never a float. A refund of
   US$84.00 is ``8400``. Floating-point money is a production incident
   waiting for a rounding boundary.
2. Every model here is a frozen dataclass. A run's history is evidence;
   evidence that can be edited in place is not evidence. Use
   :func:`dataclasses.replace` or the ``with_*`` helpers to derive a new
   value.
3. Everything must round-trip through JSON. A checkpoint you cannot
   write to disk is not a checkpoint.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field, replace
from enum import Enum
from typing import Any, Literal

__all__ = [
    "Currency",
    "Message",
    "Money",
    "Role",
    "RunState",
    "RunStatus",
    "ToolCall",
    "ToolResult",
    "ToolSpec",
]

#: Money is always an integer number of minor units (cents). Never a float.
Money = int


class Currency(str, Enum):
    """Currencies the Northstar Returns fixtures use.

    Subclassing :class:`str` keeps the value JSON-serialisable without a
    custom encoder, which matters for checkpoints and journals.
    """

    USD = "USD"
    EUR = "EUR"


Role = Literal["system", "user", "assistant", "tool"]
RunStatus = Literal[
    "running",
    "waiting_approval",
    "succeeded",
    "failed",
    "cancelled",
]


@dataclass(frozen=True)
class ToolSpec:
    """The contract for one tool, as the model and the runtime both see it.

    ``writes`` and ``idempotent`` are not documentation. The policy engine
    reads ``writes`` to decide whether a call needs an approval gate, and
    the runtime reads ``idempotent`` to decide whether a retry after a
    timeout is safe. Getting either flag wrong is how you double-refund a
    customer.

    Args:
        name: ``snake_case``, ``verb_noun``. The model sees this string.
        description: What the tool does and when to reach for it. This is
            prompt text; write it for a reader who has no other context.
        input_schema: JSON Schema for the arguments.
        output_schema: JSON Schema for the result content.
        writes: ``True`` if a successful call changes the world.
        idempotent: ``True`` if repeating the same call with the same
            arguments produces the same world state.
        max_result_tokens: Result budget. The runtime truncates past it.
        version: Bump when the schema or semantics change.
    """

    name: str
    description: str
    input_schema: dict[str, Any]
    output_schema: dict[str, Any]
    writes: bool
    idempotent: bool
    max_result_tokens: int = 800
    version: str = "1"

    def to_dict(self) -> dict[str, Any]:
        """Return the JSON-serialisable form sent to a model provider."""
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": copy.deepcopy(self.input_schema),
            "output_schema": copy.deepcopy(self.output_schema),
            "writes": self.writes,
            "idempotent": self.idempotent,
            "max_result_tokens": self.max_result_tokens,
            "version": self.version,
        }


@dataclass(frozen=True)
class ToolCall:
    """One tool invocation the model asked for.

    ``id`` correlates the call with its :class:`ToolResult`, its span, and
    its journal entry. Keep it stable across a replay or the journal stops
    lining up with the trace.
    """

    id: str
    name: str
    arguments: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        """Return the JSON-serialisable form."""
        return {
            "id": self.id,
            "name": self.name,
            "arguments": copy.deepcopy(self.arguments),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ToolCall:
        """Rebuild a call from :meth:`to_dict` output."""
        return cls(
            id=str(data["id"]),
            name=str(data["name"]),
            arguments=dict(data.get("arguments") or {}),
        )


@dataclass(frozen=True)
class ToolResult:
    """The observation that goes back to the model.

    A failed call is still an observation, not an exception. Error results
    carry ``content={"error": str, "retryable": bool}`` so the model can
    tell "try again" apart from "stop trying".
    """

    call_id: str
    ok: bool
    content: Any
    truncated: bool = False

    @property
    def error(self) -> str | None:
        """The error message when this result is a failure, else ``None``."""
        if self.ok:
            return None
        if isinstance(self.content, dict):
            return str(self.content.get("error", "unknown error"))
        return str(self.content)

    @property
    def retryable(self) -> bool:
        """Whether the runtime may safely retry the call that failed."""
        if self.ok:
            return False
        if isinstance(self.content, dict):
            return bool(self.content.get("retryable", False))
        return False

    def to_dict(self) -> dict[str, Any]:
        """Return the JSON-serialisable form."""
        return {
            "call_id": self.call_id,
            "ok": self.ok,
            "content": copy.deepcopy(self.content),
            "truncated": self.truncated,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ToolResult:
        """Rebuild a result from :meth:`to_dict` output."""
        return cls(
            call_id=str(data["call_id"]),
            ok=bool(data["ok"]),
            content=data.get("content"),
            truncated=bool(data.get("truncated", False)),
        )

    @classmethod
    def failure(
        cls,
        call_id: str,
        error: str,
        *,
        retryable: bool = False,
    ) -> ToolResult:
        """Build the standard error-shaped result."""
        return cls(
            call_id=call_id,
            ok=False,
            content={"error": error, "retryable": retryable},
        )


@dataclass(frozen=True)
class Message:
    """One entry in the conversation the model sees.

    ``content`` is deliberately ``Any``: a string for plain text, a list of
    content blocks for an assistant turn that requested tools, a dict for a
    tool observation. The runtime keeps it JSON-serialisable.
    """

    role: Role
    content: Any

    def to_dict(self) -> dict[str, Any]:
        """Return the JSON-serialisable form."""
        return {"role": self.role, "content": copy.deepcopy(self.content)}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Message:
        """Rebuild a message from :meth:`to_dict` output."""
        role: Role = data["role"]
        return cls(role=role, content=data.get("content"))

    @property
    def tool_calls(self) -> list[ToolCall]:
        """Tool calls carried by an assistant message, if any.

        Assistant messages that request tools carry content blocks of the
        form ``{"type": "tool_use", "id": ..., "name": ..., "input": ...}``.
        """
        if self.role != "assistant" or not isinstance(self.content, list):
            return []
        calls = []
        for block in self.content:
            if isinstance(block, dict) and block.get("type") == "tool_use":
                calls.append(
                    ToolCall(
                        id=str(block.get("id", "")),
                        name=str(block.get("name", "")),
                        arguments=dict(block.get("input") or {}),
                    )
                )
        return calls


@dataclass(frozen=True)
class RunState:
    """Everything needed to resume a run on a different machine.

    This is the checkpoint payload. If a field is not here, it does not
    survive a restart, and anything that does not survive a restart cannot
    be part of the agent's correctness argument.

    The dataclass is frozen, but ``messages`` is a list. Do not append to it
    in place; use :meth:`with_messages`, which copies.
    """

    run_id: str
    step: int = 0
    messages: list[Message] = field(default_factory=list)
    status: RunStatus = "running"
    budget_spent_cents: Money = 0

    def with_messages(self, *messages: Message) -> RunState:
        """Return a copy with ``messages`` appended."""
        return replace(self, messages=[*self.messages, *messages])

    def with_status(self, status: RunStatus) -> RunState:
        """Return a copy in a new status."""
        return replace(self, status=status)

    def advance(self, *, spent_cents: Money = 0) -> RunState:
        """Return a copy one step further on, charged ``spent_cents``."""
        return replace(
            self,
            step=self.step + 1,
            budget_spent_cents=self.budget_spent_cents + spent_cents,
        )

    @property
    def is_terminal(self) -> bool:
        """Whether the run has reached a state the loop will not leave."""
        return self.status in ("succeeded", "failed", "cancelled")

    @property
    def final_text(self) -> str | None:
        """The last assistant text, which is what a user would read."""
        for message in reversed(self.messages):
            if message.role != "assistant":
                continue
            if isinstance(message.content, str):
                return message.content
            if isinstance(message.content, list):
                for block in message.content:
                    if (
                        isinstance(block, dict)
                        and block.get("type") == "text"
                    ):
                        return str(block.get("text", ""))
        return None

    def to_dict(self) -> dict[str, Any]:
        """Return the JSON-serialisable checkpoint payload."""
        return {
            "run_id": self.run_id,
            "step": self.step,
            "messages": [m.to_dict() for m in self.messages],
            "status": self.status,
            "budget_spent_cents": self.budget_spent_cents,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> RunState:
        """Rebuild a run state from :meth:`to_dict` output."""
        status: RunStatus = data.get("status", "running")
        return cls(
            run_id=str(data["run_id"]),
            step=int(data.get("step", 0)),
            messages=[
                Message.from_dict(m) for m in data.get("messages", [])
            ],
            status=status,
            budget_spent_cents=int(data.get("budget_spent_cents", 0)),
        )
