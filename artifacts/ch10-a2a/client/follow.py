"""The client-side state machine, which is the piece the old table lacked.

Four branches, not two. The most common defect in A2A client code is a poll
loop that checks for one terminal value and treats everything else as "not
yet", which silently converts a ``rejected`` task into an infinite wait and
an ``input_required`` task into a customer who never hears back.

Northstar's shared Postgres row had a ``status`` column with two legal
values. ``pending`` meant the agent was thinking, and it also meant the agent
was retrying a slow database, and it also meant the agent had crashed, and it
also meant the agent had asked for a photograph at 08:50 and stopped. Three
situations, one value, and a customer waiting on all of them. Rebuilding the
handoff did not change one line of the fraud review logic. It changed the
states.

``input_required`` and ``auth_required`` **suspend** the run against its
checkpoint rather than burning turns in a poll loop, which is the Chapter 8
durability machinery doing its job across a protocol boundary. They are also
kept apart, because they resolve through completely different systems: one
routes to a person for information, the other to an authorization server for
a token. A client that merges them ends up asking a customer for an OAuth
grant, or asking an identity provider for a shipping photo.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from transport import MockTransport
from wire import AgentCard, short_label

__all__ = [
    "ACTIONS",
    "TERMINAL",
    "RunContext",
    "drive",
    "handle",
]

# v1 wire values are prefixed ProtoJSON enum names, not lowercase labels.
TERMINAL = {
    "TASK_STATE_COMPLETED",
    "TASK_STATE_FAILED",
    "TASK_STATE_CANCELED",
    "TASK_STATE_REJECTED",
}

#: The three things a client does next. Nothing else is a legal return
#: value, and a test asserts that, because "await" leaking out where
#: "suspend" belonged is the 09:14 incident.
ACTIONS: frozenset[str] = frozenset({"finish", "suspend", "await"})


@dataclass
class RunContext:
    """Where a client's two suspension paths actually go.

    In a real deployment ``ask_customer`` writes to the support inbox and
    ``request_step_up`` calls an authorization server. Here both record, so
    a test can assert that the right request reached the right system.

    Attributes:
        asked: Messages surfaced to the customer, oldest first.
        step_ups: Scope sets a step-up was requested for.
        suspensions: One record per suspension, naming which state caused
            it. A run that suspends twice on the same task for the same
            reason is a poll loop with extra steps.
    """

    run_id: str = "run-ch10"
    asked: list[dict[str, Any]] = field(default_factory=list)
    step_ups: list[list[str]] = field(default_factory=list)
    suspensions: list[dict[str, str]] = field(default_factory=list)
    finished: list[dict[str, Any]] = field(default_factory=list)

    def ask_customer(self, message: dict[str, Any]) -> None:
        """Surface the peer's request to the person who can answer it."""
        self.asked.append(dict(message))

    def request_step_up(self, scopes: list[str]) -> None:
        """Ask the authorization server for a stronger credential."""
        self.step_ups.append(list(scopes))

    def note_suspension(self, state: str) -> None:
        """Record that the run parked, and on what."""
        self.suspensions.append({"state": state, "label": short_label(state)})

    def note_finish(self, task: dict[str, Any]) -> None:
        """Record the terminal task the run ended on."""
        self.finished.append(dict(task))


def handle(task: dict[str, Any], ctx: RunContext) -> str:
    """Route one task update. Returns the next client action.

    Args:
        task: A task in ProtoJSON form, as the transport returned it.
        ctx: Where suspensions go.

    Returns:
        ``"finish"`` for a terminal task, ``"suspend"`` for one blocked on
        the caller or on an authorization server, ``"await"`` for
        ``submitted`` or ``working``.
    """
    state = task["state"]
    if state in TERMINAL:
        ctx.note_finish(task)
        return "finish"
    if state == "TASK_STATE_INPUT_REQUIRED":
        ctx.ask_customer(task["messages"][-1])   # suspend, do not poll
        ctx.note_suspension(state)
        return "suspend"
    if state == "TASK_STATE_AUTH_REQUIRED":
        ctx.request_step_up(task["required_scopes"])
        ctx.note_suspension(state)
        return "suspend"
    return "await"        # TASK_STATE_SUBMITTED or TASK_STATE_WORKING


def drive(
    task: dict[str, Any],
    ctx: RunContext,
    *,
    transport: MockTransport,
    card: AgentCard,
    tenant: str,
    max_polls: int = 12,
) -> tuple[str, dict[str, Any], list[str]]:
    """Follow one task until it finishes or the run has to park.

    The loop that :func:`handle` is the body of. It re-reads the task only
    while the action is ``await``, which is the whole difference between
    waiting on work and waiting on a person: a task in ``input_required``
    exits this function instead of being polled for the forty-one minutes
    somebody spends finding a photograph.

    Args:
        task: The task as first returned.
        ctx: Where suspensions go.
        transport: For the reads.
        card: The resolved peer card.
        tenant: The caller's tenant. Reads are scoped to it.
        max_polls: Ceiling on reads, so a peer that never settles produces
            a bounded failure rather than a hang.

    Returns:
        ``(action, task, states)`` -- the action the client stopped on, the
        last task seen, and the states it passed through, which is what the
        demo prints and the tests assert on.

    Raises:
        RuntimeError: If ``max_polls`` reads all returned ``await``.
    """
    states = [str(task["state"])]
    for _ in range(max_polls):
        action = handle(task, ctx)
        if action != "await":
            return action, task, states
        task = transport.get_task(card, str(task["id"]), tenant=tenant)
        if str(task["state"]) != states[-1]:
            states.append(str(task["state"]))
    raise RuntimeError(
        f"task {task['id']} stayed in {task['state']} for {max_polls} reads"
    )
