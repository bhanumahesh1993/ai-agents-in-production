"""The container rung, as an opt-in overlay that skips itself cleanly.

A container is a good packaging and resource boundary and a mediocre
security boundary, because every process in every container on a host
calls the same kernel. What it adds over the subprocess rung is
namespaces, a read-only root, a dropped capability set, a pid limit, and
a memory limit -- all of which this implementation sets explicitly,
because an unset limit is whatever the platform's maximum happens to be.

It is off unless you ask for it. :func:`docker_available` requires the
opt-in environment variable, a working ``docker`` CLI, *and* the image
already present locally, so that a machine with Docker installed does not
turn an offline test suite into one that pulls an image over the network.
The whole suite passes with Docker absent; these tests skip.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import time
from pathlib import Path

from netshim import CHILD_PRELUDE, NetworkConfig, split_denies
from sandbox import (
    MAX_OUTPUT_BYTES,
    TIMEOUT_EXIT_CODE,
    SandboxResult,
    clip,
)

__all__ = ["ContainerSandbox", "OPT_IN_ENV", "docker_available"]

HERE = Path(__file__).resolve().parent
PACKAGES = HERE.parents[1] / "packages"

OPT_IN_ENV = "NORTHSTAR_CH12_DOCKER"
IMAGE = os.environ.get("NORTHSTAR_CH12_IMAGE", "python:3.13-slim")
LIB_MOUNT = "/opt/sandbox-lib"
PKG_MOUNT = "/opt/northstar"
SCRATCH = "/scratch"

# Pulling a namespace and a filesystem into place is not free. The
# workload's timeout is the caller's number; this is the boot budget on
# top of it, so a slow start is not reported as the code's fault.
STARTUP_ALLOWANCE_S = 5


def docker_available() -> bool:
    """Whether the container rung may run: opt-in, CLI, and image present."""
    if os.environ.get(OPT_IN_ENV) != "1":
        return False
    if shutil.which("docker") is None:
        return False
    try:
        probe = subprocess.run(
            ["docker", "image", "inspect", IMAGE],
            capture_output=True,
            timeout=20,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return probe.returncode == 0


class ContainerSandbox:
    """``docker run`` with the namespace, capability, and limit flags set.

    ``--network none`` means the loopback stub is genuinely unreachable
    from inside, so this rung's egress test proves the policy denied
    *and* the namespace would have. The allowlist-succeeds test does not
    apply here and the suite skips it, which is why
    :attr:`can_reach_loopback` exists.
    """

    name = "container"
    can_reach_loopback = False
    is_negative_control = False

    def __init__(
        self,
        net: NetworkConfig,
        *,
        image: str = IMAGE,
        memory: str = "256m",
        pids: int = 64,
        scratch_quota_bytes: int = 1024 * 1024,
        max_output_bytes: int = MAX_OUTPUT_BYTES,
    ) -> None:
        """Build the rung. Nothing is started until :meth:`run`."""
        self._net = net
        self._image = image
        self._memory = memory
        self._pids = pids
        self._quota = scratch_quota_bytes
        self._max_output = max_output_bytes
        self._session = 0

    def _argv(self) -> list[str]:
        """The full ``docker run`` command line, with every limit named."""
        return [
            "docker", "run", "--rm", "-i",
            "--network", "none",            # no route out, at all
            "--read-only",                  # immutable root filesystem
            "--user", "65534:65534",        # nobody; never root
            "--cap-drop", "ALL",
            "--security-opt", "no-new-privileges",
            "--pids-limit", str(self._pids),
            "--memory", self._memory,
            "--tmpfs", f"{SCRATCH}:size=1m,mode=0700",
            "--workdir", SCRATCH,
            "-v", f"{HERE}:{LIB_MOUNT}:ro",
            "-v", f"{PACKAGES}:{PKG_MOUNT}:ro",
            "-e", f"NORTHSTAR_SANDBOX_LIB={LIB_MOUNT}:{PKG_MOUNT}",
            "-e", f"NORTHSTAR_SANDBOX_NET={self._net.to_json()}",
            "-e", f"NORTHSTAR_SANDBOX_FSIZE={self._quota}",
            # No RLIMIT_CPU here. Container startup costs a second or so,
            # so the wall clock has to be the timeout mechanism or a slow
            # boot would be reported as a CPU kill rather than a timeout.
            "-e", "NORTHSTAR_SANDBOX=1",
            "-e", f"NORTHSTAR_SESSION={self._session}",
            self._image,
            "python", "-I", "-c", CHILD_PRELUDE,
        ]

    def run(self, code: str, timeout_s: int) -> SandboxResult:
        """Execute ``code`` in a fresh container."""
        started = time.monotonic()
        timed_out = False
        with subprocess.Popen(
            self._argv(),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        ) as proc:
            try:
                stdout, stderr = proc.communicate(
                    code, timeout=timeout_s + STARTUP_ALLOWANCE_S
                )
            except subprocess.TimeoutExpired:
                timed_out = True
                proc.kill()
                stdout, stderr = proc.communicate()
            returncode = proc.returncode
        duration_ms = int((time.monotonic() - started) * 1000)
        stderr, denied = split_denies(stderr)
        if timed_out:
            returncode = TIMEOUT_EXIT_CODE
            stderr = f"{stderr}\ntimeout: container killed".strip()
        return SandboxResult(
            ok=returncode == 0,
            stdout=clip(stdout, self._max_output),
            stderr=clip(stderr, self._max_output),
            exit_code=returncode,
            duration_ms=duration_ms,
            denied_egress=denied,
        )

    def reset(self) -> None:
        """Start a new session.

        ``--rm`` plus a ``tmpfs`` scratch means each run already gets a
        fresh filesystem; the counter exists so a caller can see that
        sessions are distinct rather than having to trust it.
        """
        self._session += 1

    def close(self) -> None:
        """Nothing survives a run, so there is nothing to close."""
        return None
