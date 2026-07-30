"""The swappable sandbox interface, and the result every rung returns.

The interface is deliberately small. If it were larger, swapping the
isolation rung would be a rewrite rather than a configuration change, and
the whole argument of Chapter 12 -- that egress and credentials are
configuration on every rung rather than properties of any of them -- would
be untestable.

``denied_egress`` is on the result rather than in a side channel because
the deny list is evidence. It goes into the tool result, the trace span,
and the event log, and a run whose deny list is non-empty is worth a look
even when the run succeeded.

Concrete implementations also provide ``close()``, for deterministic
teardown of scratch directories in tests. That is not on the protocol
because the book prints the protocol with exactly three members.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

__all__ = [
    "DENIED_EXIT_CODE",
    "MAX_OUTPUT_BYTES",
    "ROOT_EXIT_CODE",
    "TIMEOUT_EXIT_CODE",
    "Sandbox",
    "SandboxResult",
    "clip",
]

# A wall-clock timeout is not a crash, and the difference has to survive
# the trip back to the caller. 124 is what timeout(1) reports, so the
# convention is borrowed rather than invented.
TIMEOUT_EXIT_CODE = 124

# The sandbox refused to start because it would have run as root. A
# distinct code, because "refused to run" and "ran and failed" are
# different incidents.
ROOT_EXIT_CODE = 125

# The executed code was stopped by the egress policy.
DENIED_EXIT_CODE = 126

# Output cap. The model's context window is a budget too, and a tool that
# can return a gigabyte is a denial of service on your token spend.
MAX_OUTPUT_BYTES = 64 * 1024


@dataclass(frozen=True)
class SandboxResult:
    """What one execution produced, including what it was refused.

    Args:
        ok: The code ran to completion with exit status zero.
        stdout: Captured standard output, already capped.
        stderr: Captured standard error, with the deny markers removed.
        exit_code: Process exit status, or one of the codes above.
        duration_ms: Wall-clock time the execution took.
        denied_egress: Hosts the egress policy refused, in order, for the
            log. Empty is the normal case and non-empty is a signal.
    """

    ok: bool
    stdout: str
    stderr: str
    exit_code: int
    duration_ms: int
    denied_egress: list[str]

    @property
    def timed_out(self) -> bool:
        """Whether this result is a timeout rather than a failure."""
        return self.exit_code == TIMEOUT_EXIT_CODE


class Sandbox(Protocol):
    """A bounded execution environment for model-written code."""

    name: str  # "in-process" | "subprocess" | "container" | "microvm"

    def run(self, code: str, timeout_s: int) -> SandboxResult:
        """Execute ``code``, giving up after ``timeout_s`` wall seconds."""
        ...

    def reset(self) -> None:
        """Destroy session state. Not optional: see Chapter 12."""
        ...


def clip(text: str, limit: int = MAX_OUTPUT_BYTES) -> str:
    """Cap ``text`` at ``limit`` characters, marking that it was cut."""
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n[output truncated at {limit} characters]"
