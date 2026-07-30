"""The edge deployment shape: one hibernating object per session.

An agent session is a small, long-lived, mostly idle piece of state with an
identity, which is precisely the actor model. ``storage`` holds the
per-session local store and the checkpointer adapter over it; ``session``
holds the shell that wakes on a message and sleeps when idle.

Imports are absolute throughout this artifact: the directory is on
``sys.path`` rather than being a package the repository installs, so
``import edge.session`` is what the printed ``python
artifacts/ch23-k8s-edge/demo.py`` command resolves.
"""

from __future__ import annotations

from edge.storage import LocalStore, StorageCheckpointer

__all__ = ["LocalStore", "StorageCheckpointer"]
