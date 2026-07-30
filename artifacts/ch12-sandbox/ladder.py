"""Which rungs exist on this machine, and how to build one.

The isolation ladder as a lookup table. Everything above the negative
control has to satisfy the same assertions, so the test suite asks this
module what is available and parameterises over the answer rather than
hard-coding a list that goes stale the moment somebody adds an adapter.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Callable

from container import ContainerSandbox, docker_available
from inprocess import InProcessSandbox
from microvm import StubMicroVMSandbox
from netshim import NetworkConfig
from sandbox import Sandbox
from subproc import SubprocessSandbox

__all__ = [
    "CONTROL",
    "build_ladder",
    "build_rung",
    "close_all",
    "rung_names",
]

#: The rung with no boundary. Named once, here, so nothing else has to
#: spell it and no test can silently drop it.
CONTROL = "in-process"


def _control(net: NetworkConfig) -> Sandbox:
    """The negative control, with policy enforcement removed."""
    return InProcessSandbox(dataclasses.replace(net, policy=None))


_BUILDERS: dict[str, Callable[[NetworkConfig], Sandbox]] = {
    CONTROL: _control,
    "subprocess": SubprocessSandbox,
    "microvm": StubMicroVMSandbox,
    "container": ContainerSandbox,
}


def rung_names(*, include_control: bool = False) -> list[str]:
    """Rungs runnable here, weakest first. Docker's is opt-in and may skip."""
    names = [CONTROL, "subprocess", "microvm"]
    if docker_available():
        names.append("container")
    if not include_control:
        names.remove(CONTROL)
    return names


def build_rung(name: str, net: NetworkConfig) -> Sandbox:
    """Build one rung by name."""
    try:
        builder = _BUILDERS[name]
    except KeyError:
        known = ", ".join(_BUILDERS)
        raise ValueError(f"unknown rung {name!r}; known rungs: {known}") from None
    return builder(net)


def build_ladder(
    net: NetworkConfig,
    *,
    include_control: bool = True,
) -> list[Sandbox]:
    """Build every available rung, weakest first."""
    return [
        build_rung(name, net)
        for name in rung_names(include_control=include_control)
    ]


def close_all(boxes: list[Sandbox]) -> None:
    """Release every rung's session state. Teardown is part of the design."""
    for box in boxes:
        close = getattr(box, "close", None)
        if callable(close):
            close()
