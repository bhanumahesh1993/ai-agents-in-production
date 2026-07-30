"""A stub microVM adapter. The boundary is mocked; say so out loud.

A real implementation of this rung boots a Firecracker guest with its own
kernel, so that a kernel exploit inside the guest compromises the guest's
kernel rather than yours. This one does not: it delegates to the
subprocess rung and calls itself ``microvm``.

It ships anyway, for a reason the chapter states directly. The suite runs
the same parameterised cases against every available implementation, so
adding a real microVM adapter later means proving it satisfies the same
assertions rather than trusting that a stronger rung must be safer. This
stub is the shape of that proof, present and passing, with the one claim
it cannot make written on it.

What is real here: per-session filesystem destruction, the scrubbed
environment, the wall-clock timeout, the scratch quota, and the egress
deny. What is mocked: the hardware virtualization boundary, the guest
kernel, and the boot. There is no VM.
"""

from __future__ import annotations

from netshim import NetworkConfig
from sandbox import MAX_OUTPUT_BYTES, SandboxResult
from subproc import DEFAULT_SCRATCH_QUOTA, SubprocessSandbox

__all__ = ["StubMicroVMSandbox"]


class StubMicroVMSandbox:
    """The subprocess rung wearing a microVM's name, and admitting it."""

    name = "microvm"
    can_reach_loopback = True
    is_negative_control = False

    #: There is no hypervisor here. Read this attribute before you trust
    #: a green test on this rung for a kernel-boundary claim.
    provides_hardware_boundary = False

    def __init__(
        self,
        net: NetworkConfig,
        *,
        scratch_quota_bytes: int = DEFAULT_SCRATCH_QUOTA,
        max_output_bytes: int = MAX_OUTPUT_BYTES,
    ) -> None:
        """Build the stub over a real subprocess rung."""
        self._inner = SubprocessSandbox(
            net,
            scratch_quota_bytes=scratch_quota_bytes,
            max_output_bytes=max_output_bytes,
        )

    def run(self, code: str, timeout_s: int) -> SandboxResult:
        """Execute ``code``. No guest boots; the process is the guest."""
        return self._inner.run(code, timeout_s)

    def reset(self) -> None:
        """Destroy the session. A real adapter would destroy the VM."""
        self._inner.reset()

    def close(self) -> None:
        """Release the session's scratch directory."""
        self._inner.close()
