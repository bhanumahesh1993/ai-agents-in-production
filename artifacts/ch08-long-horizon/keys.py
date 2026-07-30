"""Two ways to identify a refund, and why only one of them survives.

The whole chapter turns on a few characters of difference, so the artifact
ships both and lets you watch the second one fail.

::

    # Correct: derived from values the checkpoint already holds.
    key = idempotency_key(state.run_id, req.step_id)

    # Broken: a new identity for the same intent. If the pre-crash
    # attempt committed under a different key, the target system
    # cannot match them, and the resume issues a second refund.
    key = uuid4().hex

The generated version looks equivalent and is not. It introduces a window
between creating the key and durably persisting it, and that window is
precisely where a crash produces a duplicate write, because the pre-crash
attempt used a key no surviving record knows about. Derivation removes the
window: the key never exists in memory but not in storage, because it never
needed storing.

Two smaller rules keep derived keys honest, and :func:`derived_key`
enforces the first by construction.

**``step_id`` must be stable across resume.** Take it from the append-only
journal position, which is durable and monotonic. An in-memory counter
that resets, a position in a message list that compaction rewrites, or a
retry count all produce a different key for the same logical action on the
second attempt, and idempotency silently stops working.

**The key must reach the target system's own uniqueness constraint.** A key
the agent computes and the payment service ignores guarantees nothing; it
only makes the trace look responsible. :mod:`side_effects` is where that
constraint actually lives, as a ``UNIQUE`` index.
"""

from __future__ import annotations

from uuid import uuid4

from northstar_contracts import idempotency_key

__all__ = ["KEY_STRATEGIES", "derived_key", "generated_key", "key_for"]

#: The two strategies, named so a demo flag can select one.
KEY_STRATEGIES: tuple[str, ...] = ("derived", "generated")


def derived_key(run_id: str, step_id: int) -> str:
    """The correct strategy: a pure function of persisted identifiers.

    Any process that loads the checkpoint recomputes this without ever
    having stored it, which is exactly the property a resumed run needs.

    Args:
        run_id: The run's identifier, already in the checkpoint.
        step_id: The journal position of the step, already in the
            checkpoint. Not a call id, not a retry count, not a wall clock.

    Returns:
        A 32-character hex string, identical on every attempt.
    """
    return idempotency_key(run_id, step_id)


def generated_key() -> str:
    """The broken strategy, kept so the failure is reproducible.

    A nonce. It identifies an attempt rather than an intent, so a second
    worker presenting a second nonce is, as far as the target system can
    tell, asking for a second refund.
    """
    return uuid4().hex


def key_for(run_id: str, step_id: int, strategy: str = "derived") -> str:
    """Select a strategy by name.

    Raises:
        ValueError: On an unknown strategy. There is no default that
            quietly falls back to generation.
    """
    if strategy == "derived":
        return derived_key(run_id, step_id)
    if strategy == "generated":
        return generated_key()
    raise ValueError(
        f"unknown key strategy {strategy!r}; expected one of "
        f"{', '.join(KEY_STRATEGIES)}"
    )
