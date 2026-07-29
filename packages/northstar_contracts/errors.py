"""Exceptions tool implementations raise, and what the runtime does with them.

The runtime never lets a tool exception escape the agent loop. It converts
the exception into a :class:`~northstar_contracts.models.ToolResult` with
``ok=False`` and lets the model decide what to do next. The only thing the
runtime needs from the exception is one bit: *may this be retried?*

That bit is the class, not a string. ``RetryableToolError`` means the call
may not have landed, or landed and can be safely repeated. Anything else
means stop.
"""

from __future__ import annotations

__all__ = [
    "RetryableToolError",
    "ToolError",
    "ToolNotFound",
    "ToolPermissionError",
    "ToolTimeout",
    "ToolValidationError",
]


class ToolError(Exception):
    """Base class for a tool failure. Not retryable by default."""

    retryable: bool = False


class RetryableToolError(ToolError):
    """A failure that may succeed on a second attempt.

    Careful: retryable does not mean safe. A timeout on a write is
    retryable *only* if the write carries an idempotency key. That
    distinction is the Chapter 1 incident in one sentence.
    """

    retryable = True


class ToolTimeout(RetryableToolError):
    """The call did not return in time.

    The dangerous case, and the reason this class exists separately: the
    write may already have landed on the server. The caller cannot tell the
    difference between "never arrived" and "arrived, reply lost".
    """


class ToolValidationError(ToolError):
    """The arguments did not satisfy the tool's input schema."""


class ToolPermissionError(ToolError):
    """The principal is not allowed to make this call."""


class ToolNotFound(ToolError):
    """The model asked for a tool that is not in the registry."""
