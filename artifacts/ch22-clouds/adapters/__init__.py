"""Cloud adapters behind one four-method interface.

``base`` holds the interface. ``mock`` is the one the demo and the test
suite run against, because a scorecard you cannot reproduce without three
cloud accounts is a scorecard nobody re-runs. The three real adapters are
importable, readable, and offline: nothing here imports a cloud SDK.

Imports are absolute throughout this artifact, matching every other
chapter: the directory is on sys.path rather than being a package the
repository installs, so import adapters.aws is what the printed
python artifacts/ch22-clouds/demo.py command resolves.
"""

from __future__ import annotations

from adapters.base import (
    ADAPTER_METHODS,
    PORTABLE,
    CloudAdapter,
    CloudUnavailable,
    ExitCost,
    extra_methods,
)

__all__ = [
    "ADAPTER_METHODS",
    "PORTABLE",
    "CloudAdapter",
    "CloudUnavailable",
    "ExitCost",
    "extra_methods",
]
