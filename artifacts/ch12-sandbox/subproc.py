"""The first real rung: a separate process, scrubbed and bounded.

The code runs in its own interpreter, in its own process group, with an
environment built from an allowlist, a scratch directory that is deleted
on ``reset()``, a kernel-enforced file-size quota, a wall-clock timeout
that kills the whole process group, an output cap, and the egress hook in
front of ``urlopen``.

What this rung does not have is syscall filtering. seccomp-BPF is Linux
only and this repository has to run on a laptop, so the artifact stops at
the controls that are portable and honest, and the README names the gap
rather than implying it is covered. What it does have is every control on
the *configuration* side of the chapter's argument -- filesystem, egress,
secrets, time -- and those are the ones that do not improve as you climb.
"""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from broker import scrub_env
from netshim import CHILD_PRELUDE, NetworkConfig, split_denies
from sandbox import (
    MAX_OUTPUT_BYTES,
    ROOT_EXIT_CODE,
    TIMEOUT_EXIT_CODE,
    SandboxResult,
    clip,
)

__all__ = ["SubprocessSandbox", "library_path"]

HERE = Path(__file__).resolve().parent
DEFAULT_SCRATCH_QUOTA = 256 * 1024


def library_path() -> str:
    """The directories the child needs on ``sys.path``, path-separated.

    The artifact directory, for the shim, and this repository's
    ``packages/`` if it is not installed. Nothing else: the child gets the
    code that enforces the policy and no path back into the agent.
    """
    parts = [str(HERE)]
    packages = HERE.parents[1] / "packages"
    if packages.is_dir():
        parts.append(str(packages))
    return os.pathsep.join(parts)


class SubprocessSandbox:
    """A separate ``python -c`` process, with the four surfaces set."""

    name = "subprocess"
    can_reach_loopback = True
    is_negative_control = False

    def __init__(
        self,
        net: NetworkConfig,
        *,
        scratch_quota_bytes: int = DEFAULT_SCRATCH_QUOTA,
        max_output_bytes: int = MAX_OUTPUT_BYTES,
        python: str | None = None,
    ) -> None:
        """Build the rung.

        Args:
            net: The egress policy and the offline routes.
            scratch_quota_bytes: Hard ``RLIMIT_FSIZE`` for the child, so
                the quota is enforced by the kernel rather than by a
                check the code could skip.
            max_output_bytes: Cap on captured stdout and stderr.
            python: Interpreter to run. Defaults to this one.
        """
        self._net = net
        self._quota = scratch_quota_bytes
        self._max_output = max_output_bytes
        self._python = python or sys.executable
        self._scratch = Path(tempfile.mkdtemp(prefix="ns-ch12-subproc-"))

    @property
    def scratch(self) -> Path:
        """The session's writable directory. Replaced by ``reset()``."""
        return self._scratch

    def run(self, code: str, timeout_s: int) -> SandboxResult:
        """Execute ``code`` in a fresh child process."""
        if hasattr(os, "geteuid") and os.geteuid() == 0:
            return SandboxResult(
                ok=False,
                stdout="",
                stderr=(
                    "refusing to execute as root: a base image with no "
                    "USER line is one kernel bug from root outside it"
                ),
                exit_code=ROOT_EXIT_CODE,
                duration_ms=0,
                denied_egress=[],
            )
        env = scrub_env(
            os.environ,
            extra={
                "HOME": str(self._scratch),
                "TMPDIR": str(self._scratch),
                "NORTHSTAR_SANDBOX": "1",
                "NORTHSTAR_SANDBOX_LIB": library_path(),
                "NORTHSTAR_SANDBOX_NET": self._net.to_json(),
                "NORTHSTAR_SANDBOX_FSIZE": str(self._quota),
                "NORTHSTAR_SANDBOX_CPU": str(timeout_s + 1),
            },
        )
        started = time.monotonic()
        timed_out = False
        with subprocess.Popen(
            [self._python, "-I", "-c", CHILD_PRELUDE],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=str(self._scratch),
            env=env,
            text=True,
            # Its own process group, so the timeout kill takes whatever
            # the code started along with it.
            start_new_session=True,
        ) as proc:
            try:
                stdout, stderr = proc.communicate(code, timeout=timeout_s)
            except subprocess.TimeoutExpired:
                timed_out = True
                self._kill_group(proc)
                stdout, stderr = proc.communicate()
            returncode = proc.returncode
        duration_ms = int((time.monotonic() - started) * 1000)
        stderr, denied = split_denies(stderr)
        if timed_out:
            returncode = TIMEOUT_EXIT_CODE
            stderr = (
                f"{stderr}\ntimeout: killed after {timeout_s}s of wall clock"
            ).strip()
        elif returncode < 0:
            # Killed by a signal -- the file-size quota, the CPU limit, or
            # the OOM killer. Report it as a status, not as a crash.
            stderr = f"{stderr}\nterminated by signal {-returncode}".strip()
            returncode = 128 + (-returncode)
        return SandboxResult(
            ok=returncode == 0,
            stdout=clip(stdout, self._max_output),
            stderr=clip(stderr, self._max_output),
            exit_code=returncode,
            duration_ms=duration_ms,
            denied_egress=denied,
        )

    @staticmethod
    def _kill_group(proc: subprocess.Popen[str]) -> None:
        """Kill the child and anything it started."""
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            proc.kill()

    def reset(self) -> None:
        """Destroy the session's filesystem and start a new one.

        Terminating a container is not the same as removing its volumes.
        This is the removal, and the test asserts a file written before
        the call is absent after it.
        """
        shutil.rmtree(self._scratch, ignore_errors=True)
        self._scratch = Path(tempfile.mkdtemp(prefix="ns-ch12-subproc-"))

    def close(self) -> None:
        """Remove the scratch directory for good."""
        shutil.rmtree(self._scratch, ignore_errors=True)
