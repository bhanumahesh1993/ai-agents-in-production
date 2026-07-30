"""Version pinning, and the transformer that migrates a parked run.

Four strategies are legitimate and the requirement is to choose one per
agent and write it down. This module implements two of them and names the
other two, because "we will decide at the time" is the strategy that
produced the opening incident.

*Pin to the original version.* :func:`plan` returns ``"pin"`` when the
worker is already running what the checkpoint was written by. Simplest
correct answer, and it requires keeping old versions runnable for your
maximum run duration plus margin.

*Migrate through an explicit transformer.* :data:`MIGRATIONS` maps a
version pair to a tested function from the old envelope to the new one, run
during ``resuming``. There is no generic transformer and no reflection: a
migration nobody wrote is a migration nobody tested.

*Cancel and restart* is honest for short runs and unacceptable for a run
holding a three-day-old approval. *Dual-compatible readers* are cheap for
additive changes and dangerous as a default, because "additive" is a
judgment and the field someone adds is eventually load-bearing.

The v7-to-v8 transformer here is the incident, reproduced. It adds a
``notify_channel`` field to the pending call, which is additive by every
schema rule and harmless by every compatibility test. It also changes the
canonical JSON of that call, which changes its approval fingerprint, which
invalidates a decision a human already made. That is a semantic
incompatibility hiding inside a compatible schema change, and the defence
is not a better schema check. It is a resume path with a branch for it.
"""

from __future__ import annotations

from collections.abc import Callable

from envelope import Envelope

__all__ = [
    "MIGRATIONS",
    "NoMigrationPath",
    "V7",
    "V8",
    "migrate",
    "plan",
    "v7_to_v8",
]

V7 = "v7"
V8 = "v8"


class NoMigrationPath(RuntimeError):
    """No declared transformer from the stored version to this one."""

    def __init__(self, source: str, target: str) -> None:
        self.source = source
        self.target = target
        known = ", ".join(f"{a}->{b}" for a, b in sorted(MIGRATIONS))
        super().__init__(
            f"no migration from {source!r} to {target!r}; declared paths: "
            f"{known or '(none)'}. Pin the old version or cancel the run."
        )


def v7_to_v8(envelope: Envelope) -> Envelope:
    """Add the field the release added, and say so.

    ``channel`` was added to ``send_message`` so support could tell email
    from SMS. Reasonable change, reviewed, additive, shipped on a Saturday.
    The pending call carries its equivalent here, and adding it is what
    breaks the fingerprint.
    """
    envelope.agent_version = V8
    if envelope.pending_call is not None:
        arguments = dict(envelope.pending_call.get("arguments") or {})
        arguments.setdefault("notify_channel", "email")
        envelope.pending_call = {
            **envelope.pending_call,
            "arguments": arguments,
        }
    return envelope


#: Declared, tested transformers. Keyed by ``(from_version, to_version)``.
MIGRATIONS: dict[tuple[str, str], Callable[[Envelope], Envelope]] = {
    (V7, V8): v7_to_v8,
}


def plan(stored: str, running: str) -> str:
    """What the resume path should do about a version difference.

    Returns:
        ``"pin"`` when the versions match, ``"migrate"`` when a
        transformer is declared, and ``"refuse"`` otherwise. Three answers
        and no fourth: there is no "probably fine".
    """
    if stored == running:
        return "pin"
    if (stored, running) in MIGRATIONS:
        return "migrate"
    return "refuse"


def migrate(envelope: Envelope, to: str) -> Envelope:
    """Run the declared transformer, or raise.

    Raises:
        NoMigrationPath: When no transformer is declared. Never guess: a
            run deserialised into code that was not written for it has been
            silently migrated by nobody.
    """
    transformer = MIGRATIONS.get((envelope.agent_version, to))
    if transformer is None:
        raise NoMigrationPath(envelope.agent_version, to)
    return transformer(envelope)
