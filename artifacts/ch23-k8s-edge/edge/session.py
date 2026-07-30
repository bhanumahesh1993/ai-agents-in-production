"""One object per session, and what hibernation costs. Nothing.

Consider what this does to the problem that broke the cluster deployment.
An agent waiting four hours for a human is asleep. It occupies no worker,
holds no connection, and costs nothing for those four hours. It wakes when
the message arrives, reads its own state from local storage, and continues.
There is no queue lease to renew, no pod to keep alive, no autoscaler to
mislead, and no session to lose in a rolling deploy.

The constraints are real and they decide the fit. Per-invocation CPU and
memory ceilings are far lower than a pod's, so long CPU-bound work, a local
vector index, or a heavyweight framework will not fit. The model call still
leaves the edge unless you use the platform's own inference. And the
runtime is not a general container, so an existing Python agent does not
lift and shift — which is precisely why the decision logic lives in
:mod:`agent_builder` and this file is a shell.

One thing this module is careful about, because getting it wrong would
make the whole demonstration a lie: the **world is not session state**. The
refund service lives outside the object and outlives every hibernation. The
object's local storage holds where the *run* got to. Conflating them would
let a woken session refund a customer a second time and report that state
had survived.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from agent_builder import ORDER, build_support_agent
from northstar_contracts import RunState, World

from edge.storage import LocalStore, StorageCheckpointer

__all__ = ["Hibernated", "SupportSession", "hibernate_and_wake"]


class Hibernated(RuntimeError):
    """The object went to sleep at a step boundary.

    Raised by the hibernation hook, which the runtime calls *after* the
    step is durably checkpointed. Sleeping between steps rather than inside
    one is the same discipline a kill switch needs: an interruption in the
    middle of a write leaves the ambiguity the whole book is about.
    """


class SupportSession:
    """One object per session. The agent logic is imported, not rewritten."""

    def __init__(
        self,
        session_id: str,
        storage: LocalStore,
        world: World | None = None,
        hibernate_after_step: int | None = None,
    ) -> None:
        self.session_id = session_id
        self.storage = storage          # per-session, local to this object
        # The refund service. Outside the object, and outlives it.
        self.world = world if world is not None else World()
        self.loop = build_support_agent(   # the same builder as everywhere
            self.world,
            checkpointer=StorageCheckpointer(storage),
            run_id=session_id,
        )
        if hibernate_after_step is not None:
            self.loop.step_hook = _sleep_at(hibernate_after_step)
        #: How many times this object has been woken, and slept.
        self.wakes = 0
        self.hibernations = 0

    # ------------------------------------------------------------ lifecycle

    def on_message(self, text: str) -> str:
        """Handle one inbound message, resuming if this session has state."""
        self.wakes += 1
        try:
            state = self.resume_or_start(text)
        except Hibernated:
            # The step was checkpointed before the hook ran, so the durable
            # record is already correct and there is nothing to clean up.
            state = self.loop.checkpointer.load(self.session_id) or RunState(
                run_id=self.session_id
            )
        self.storage.put("state", state.to_dict())   # survives hibernation
        return state.final_text or ""

    def on_hibernate(self) -> None:
        """Nothing to do. State is already durable. That is the point."""
        self.hibernations += 1

    def on_wake(self) -> RunState | None:
        """What the object knows about itself after sleeping."""
        return self.loop.checkpointer.load(self.session_id)

    def resume_or_start(self, text: str) -> RunState:
        """Continue the stored run, or begin one.

        The runtime contract exposes ``start`` and ``resume`` rather than a
        combined verb, so the shell owns the join. Three branches, and the
        last is the one that matters: a resumed session picks up the
        checkpoint it wrote before it slept rather than starting a second
        run, which would refund the customer again and report success.
        """
        stored = self.loop.checkpointer.load(self.session_id)
        if stored is None:
            return self.loop.run(text, run_id=self.session_id)
        if stored.is_terminal:
            return stored
        return self.loop.resume(stored)

    # -------------------------------------------------------------- queries

    @property
    def refund_rows(self) -> int:
        """Refunds the service holds. The number that must stay 1."""
        return len(self.world.refunds_for(ORDER))

    @property
    def refunded_cents(self) -> int:
        """What landed, not what the agent said."""
        return self.world.total_refunded_cents(ORDER)

    def snapshot(self) -> dict[str, Any]:
        """Everything a reviewer would want after a wake."""
        state = self.on_wake()
        return {
            "session_id": self.session_id,
            "wakes": self.wakes,
            "hibernations": self.hibernations,
            "status": state.status if state else "unknown",
            "step": state.step if state else 0,
            "storage_keys": self.storage.keys(),
            "storage_writes": self.storage.writes,
            "refund_rows": self.refund_rows,
            "refunded_cents": self.refunded_cents,
        }


def _sleep_at(step: int) -> Callable[[RunState], None]:
    """Build a step hook that hibernates once the given step has committed."""

    def hook(state: RunState) -> None:
        if state.step >= step and not state.is_terminal:
            raise Hibernated(
                f"session {state.run_id} hibernating after step {state.step}"
            )

    return hook


def hibernate_and_wake(
    session_id: str,
    storage: LocalStore,
    world: World,
    hibernate_after_step: int = 3,
) -> tuple[SupportSession, list[dict[str, Any]]]:
    """Drive one session through a hibernation and a wake.

    A **new** :class:`SupportSession` is constructed for the second
    message. That is the fidelity that matters: hibernation destroys the
    object's memory and keeps its storage, so building a fresh shell over
    the same store is what the platform actually does, and it is the only
    way "state survived" is worth anything. The world is passed in for the
    same reason in the other direction: the refund service was never part
    of the object.

    Args:
        session_id: The object's durable identity.
        storage: The per-session store, which outlives the object.
        world: The refund service, which outlives the object too.
        hibernate_after_step: Sleep once this step has committed. The
            default is the step the refund lands on, which is the only
            interesting place to fall asleep.

    Returns:
        The woken session and one snapshot per message.
    """
    snapshots: list[dict[str, Any]] = []

    asleep = SupportSession(
        session_id, storage, world, hibernate_after_step=hibernate_after_step
    )
    asleep.on_message("Customer reports a cracked lamp shade.")
    asleep.on_hibernate()
    snapshots.append(asleep.snapshot())

    # The object is gone. Its storage and the refund service are not.
    awake = SupportSession(session_id, storage, world)
    awake.wakes = asleep.wakes
    awake.hibernations = asleep.hibernations
    awake.on_message("Any update?")
    snapshots.append(awake.snapshot())
    return awake, snapshots
