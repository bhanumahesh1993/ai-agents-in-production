"""The negative control: no boundary at all.

THIS IS NOT A SANDBOX. It runs model-written code with ``exec()`` inside
the calling process, which means the code shares your memory, your file
descriptors, your environment variables, and your credentials. Every
attempt to build a safe Python subset by removing builtins has been
defeated by attribute traversal, and this implementation does not even
try.

It is here for one reason: it is the only rung at which the chapter's
row-41 payload succeeds. A test suite in which every implementation
denies the metadata read proves nothing unless one of them can reach it,
because "denied" and "unreachable" look identical from the outside. The
demo asserts the inversion: this one must succeed, and every other rung
must not.
"""

from __future__ import annotations

import contextlib
import io
import time
import traceback

import netshim
from netshim import NetworkConfig
from sandbox import MAX_OUTPUT_BYTES, SandboxResult, clip

__all__ = ["InProcessSandbox"]


class InProcessSandbox:
    """``exec()`` in the agent's own process. Contains nothing."""

    name = "in-process"
    can_reach_loopback = True
    is_negative_control = True

    def __init__(self, net: NetworkConfig | None = None) -> None:
        """Build the control.

        Args:
            net: Network configuration. Its ``policy`` is expected to be
                ``None``: a control that enforced the policy would not be
                a control.
        """
        self._net = net or NetworkConfig(policy=None)
        self._denied: list[str] = []

    def run(self, code: str, timeout_s: int) -> SandboxResult:
        """Execute ``code`` here, in this process, with no isolation.

        ``timeout_s`` is accepted and ignored, which is itself a finding:
        there is no way to interrupt arbitrary Python running on your own
        thread, so the timeout control does not exist at this rung.
        """
        del timeout_s  # no boundary means no enforcement point
        self._denied = []
        out, err = io.StringIO(), io.StringIO()
        undo = netshim.install(self._net, self._denied.append)
        started = time.monotonic()
        exit_code = 0
        try:
            with (
                contextlib.redirect_stdout(out),
                contextlib.redirect_stderr(err),
            ):
                exec(  # noqa: S102 - the point of the negative control
                    compile(code, "<in-process>", "exec"),
                    {"__name__": "__main__"},
                )
        except BaseException:  # noqa: BLE001 - report, never propagate
            exit_code = 1
            err.write(traceback.format_exc())
        finally:
            undo()
        duration_ms = int((time.monotonic() - started) * 1000)
        return SandboxResult(
            ok=exit_code == 0,
            stdout=clip(out.getvalue(), MAX_OUTPUT_BYTES),
            stderr=clip(err.getvalue(), MAX_OUTPUT_BYTES),
            exit_code=exit_code,
            duration_ms=duration_ms,
            denied_egress=list(self._denied),
        )

    def reset(self) -> None:
        """Clear the deny log, which is all there is to clear.

        There is no session state to destroy because there was no session:
        anything the code wrote, imported, or monkeypatched happened in
        the parent process and is still there.
        """
        self._denied = []

    def close(self) -> None:
        """Nothing to close. Present so every rung teardown looks alike."""
        return None
