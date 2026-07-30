"""The A2A server adapter: every duty of a public API, at one boundary.

An A2A caller is an untrusted client that happens to speak your protocol.
So this adapter does the seven things any public endpoint does -- verify the
delegation, authorize the specific skill, resolve and namespace the tenant,
validate the payload, apply a quota, bound the work, write an audit record
-- and only then hands anything to :class:`peer.fraud_review.FraudReviewAgent`.

Two kinds of refusal, and the line between them is deliberate.

**Refused before a task exists.** No tenant, no task id, or a credential
this server will not accept. These raise :class:`AdmissionRefused`, because
a caller who was not authenticated does not get a task id back to poll, and
a request with no tenant cannot be keyed into a store that is partitioned
by tenant.

**Refused as a task in ``rejected``.** A malformed payload, a skill this
agent does not offer, an order it does not hold, a missing restated
constraint, or a tenant over quota. The caller gets a real task in a
terminal state, which is what lets the client's state machine treat it as
"never retry this payload" rather than "not yet".

The task store is keyed by ``(tenant, task_id)``. A server that accepts a
client-suggested task id without namespacing it has handed one caller a way
to read another caller's task.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from wire import (
    HANDOFF_FIELDS,
    TASK_STATE_AUTH_REQUIRED,
    TASK_STATE_CANCELED,
    TASK_STATE_INPUT_REQUIRED,
    TASK_STATE_REJECTED,
    IllegalTransition,
    Task,
)

from peer.fraud_review import (
    EVIDENCE_ARTIFACT,
    REQUIRED_ASSURANCE,
    FraudReviewAgent,
    published_card,
)

__all__ = [
    "AUDIENCE",
    "MAX_OPEN_TASKS_PER_TENANT",
    "RAW_TOKEN_KEYS",
    "REQUIRED_SCOPE",
    "A2AServer",
    "AdmissionRefused",
    "Caller",
    "evidence_message",
    "step_up_message",
]

#: The audience a delegation must be minted for. A grant that names another
#: audience is a grant this server is not the intended receiver of, however
#: valid it is elsewhere.
AUDIENCE = "northstar-fraud-review"

#: The scope the card advertises and this server requires.
REQUIRED_SCOPE = "fraud.review.submit"

#: Keys that mean the caller forwarded a credential instead of a delegation.
#: Refused outright: a receiver holding the sender's token is a confused
#: deputy with the sender's full authority and no audit trail.
RAW_TOKEN_KEYS: frozenset[str] = frozenset(
    {"access_token", "bearer", "authorization", "token", "id_token"}
)

#: Concurrent open tasks one tenant may hold. A quota is not politeness; it
#: is the only thing between one noisy caller and every other tenant.
MAX_OPEN_TASKS_PER_TENANT = 8


class AdmissionRefused(RuntimeError):
    """The request never became a task.

    Raised for identity problems only. Everything the caller could fix by
    sending a different payload comes back as a ``rejected`` task instead,
    so the client's lifecycle handling covers it.
    """


@dataclass(frozen=True)
class Caller:
    """Who this server decided the caller is, after checking the delegation.

    Built by the server from the delegation, never from the transport. The
    tenant in particular is read from the payload rather than inferred from
    the credential, because the day a shared service identity appears every
    inferred tenant collapses into one.
    """

    tenant: str
    task_id: str
    subject: str
    actor: str
    scopes: frozenset[str]
    assurance: str
    chain: tuple[str, ...]


class A2AServer:
    """The wire-facing half of the fraud review agent.

    Args:
        agent: The reasoning half. A fresh one by default, with its own
            world, so nothing is shared between server instances.
        clock: Injectable seconds counter, so expiry is testable without
            sleeping. Called once per operation.

    Attributes:
        audit: One record per operation, in order. This is what an auditor
            reads, and it names both principals on every delegated action.
        tasks: Task store, keyed by ``"{tenant}::{task_id}"``.
    """

    def __init__(
        self,
        agent: FraudReviewAgent | None = None,
        *,
        clock: Callable[[], float] | None = None,
        now: float = 0.0,
    ) -> None:
        self.agent = agent or FraudReviewAgent()
        self.tasks: dict[str, Task] = {}
        self.audit: list[dict[str, Any]] = []
        self._clock = clock
        self._now = now

    @property
    def reviews_opened(self) -> int:
        """How many fraud reviews the agent behind this adapter started.

        Surfaced here because it is the number the idempotency property is
        about, and a caller asserting on it should not have to reach through
        the adapter into the agent to find it.
        """
        return self.agent.reviews_opened

    @property
    def checks_run(self) -> int:
        """How many times the agent's signal checks ran."""
        return self.agent.checks_run

    # ------------------------------------------------------------ discovery

    def agent_card(self) -> tuple[dict[str, Any], str]:
        """The card body and its detached signature.

        The public card advertises the skill and the required scope, and
        nothing about the checks the agent runs. A complete enumeration of
        an agent's capabilities is reconnaissance.
        """
        return published_card()

    # -------------------------------------------------------------- the ops

    def send_task(self, delegation: dict[str, Any]) -> dict[str, Any]:
        """Accept a delegation, or refuse it, and return the task.

        Idempotent on ``(tenant, task_id)``. A resent delegation rejoins the
        existing task: no second review is opened, no second hold is placed
        on the customer's refund, and
        :attr:`peer.fraud_review.FraudReviewAgent.reviews_opened` does not
        move.

        Args:
            delegation: The payload the caller sent.

        Returns:
            The task, in ProtoJSON form.

        Raises:
            AdmissionRefused: On an identity problem. See the module
                docstring for why that is not a ``rejected`` task.
        """
        caller = self._authenticate(delegation)
        key = self._key(caller.tenant, caller.task_id)

        existing = self.tasks.get(key)
        if existing is not None:
            self._record("send_task", caller, "rejoined", existing.state)
            return existing.to_wire()

        problem = self._decline(delegation, caller)
        if problem is not None:
            task = Task(
                id=caller.task_id,
                tenant=caller.tenant,
                skill=str(delegation.get("skill", "")),
                state=TASK_STATE_REJECTED,
            )
            task.messages.append({"role": "agent", "text": problem})
            self.tasks[key] = task
            self._record("send_task", caller, "rejected", task.state, problem)
            return task.to_wire()

        task = Task(
            id=caller.task_id,
            tenant=caller.tenant,
            skill=str(delegation["skill"]),
        )
        self.tasks[key] = task
        self.agent.open(task, self._claim(delegation), caller.assurance)
        self._record("send_task", caller, "accepted", task.state)
        return task.to_wire()

    def get_task(self, tenant: str, task_id: str) -> dict[str, Any]:
        """Read one task. Scoped to the tenant, always.

        Advancing the agent by one node here is the mock standing in for
        time passing: a real peer works on its own schedule and a read shows
        you how far it got. It keeps the read semantically a read -- no
        argument the caller supplies changes what happens -- and it is the
        one place this transport is not a faithful model of an HTTP one.

        Raises:
            AdmissionRefused: If this tenant holds no such task. Note what
                the message does *not* say: whether some other tenant does.
        """
        task = self.tasks.get(self._key(tenant, task_id))
        if task is None:
            raise AdmissionRefused(
                f"tenant {tenant!r} holds no task {task_id!r}"
            )
        self.agent.tick(task)
        return task.to_wire()

    def send_message(
        self,
        tenant: str,
        task_id: str,
        message: dict[str, Any],
    ) -> dict[str, Any]:
        """Give a waiting task what it asked for, and let it continue.

        Two shapes, matching the two blocking states. A message carrying
        ``artifact`` answers ``input_required``; a message carrying
        ``assurance`` answers ``auth_required``. Sending the wrong one is an
        error rather than a no-op, because a client that resolves an
        authorization block by uploading a photograph has misread the
        lifecycle and should find out.

        Raises:
            AdmissionRefused: If the tenant holds no such task.
            IllegalTransition: If the task is terminal, or is not waiting
                for the thing this message supplies.
        """
        key = self._key(tenant, task_id)
        task = self.tasks.get(key)
        if task is None:
            raise AdmissionRefused(
                f"tenant {tenant!r} holds no task {task_id!r}"
            )
        if task.is_terminal:
            raise IllegalTransition(
                f"task {task_id} is {task.state}; terminal tasks do not "
                "accept new messages."
            )

        artifact = message.get("artifact")
        assurance = message.get("assurance")
        if artifact:
            if task.state != TASK_STATE_INPUT_REQUIRED:
                raise IllegalTransition(
                    f"task {task_id} is {task.state}, not "
                    f"{TASK_STATE_INPUT_REQUIRED}; an artifact does not "
                    "unblock it."
                )
            task.add_message("user", str(message.get("text", artifact)))
            self.agent.supply_evidence(task, str(artifact))
        elif assurance:
            if task.state != TASK_STATE_AUTH_REQUIRED:
                raise IllegalTransition(
                    f"task {task_id} is {task.state}, not "
                    f"{TASK_STATE_AUTH_REQUIRED}; a stronger credential "
                    "does not unblock it."
                )
            self.agent.step_up(task, str(assurance))
        else:
            raise IllegalTransition(
                "a message to a blocked task must carry either 'artifact' "
                f"(for {TASK_STATE_INPUT_REQUIRED}) or 'assurance' (for "
                f"{TASK_STATE_AUTH_REQUIRED})."
            )
        self.audit.append(
            {
                "op": "send_message",
                "tenant": tenant,
                "task_id": task_id,
                "outcome": "accepted",
                "state": task.state,
            }
        )
        return task.to_wire()

    def cancel_task(self, tenant: str, task_id: str) -> dict[str, Any]:
        """Cancel a non-terminal task.

        Raises:
            AdmissionRefused: If the tenant holds no such task.
            IllegalTransition: If the task has already ended.
        """
        key = self._key(tenant, task_id)
        task = self.tasks.get(key)
        if task is None:
            raise AdmissionRefused(
                f"tenant {tenant!r} holds no task {task_id!r}"
            )
        task.move_to(TASK_STATE_CANCELED, note="cancelled by caller")
        self.audit.append(
            {
                "op": "cancel_task",
                "tenant": tenant,
                "task_id": task_id,
                "outcome": "cancelled",
                "state": task.state,
            }
        )
        return task.to_wire()

    def tasks_for(self, tenant: str) -> list[Task]:
        """Every task one tenant holds, in creation order."""
        prefix = f"{tenant}::"
        return [t for k, t in self.tasks.items() if k.startswith(prefix)]

    def open_tasks_for(self, tenant: str) -> list[Task]:
        """The tenant's tasks that have not reached a terminal state."""
        return [t for t in self.tasks_for(tenant) if not t.is_terminal]

    # ----------------------------------------------------------- internals

    @staticmethod
    def _key(tenant: str, task_id: str) -> str:
        """Namespace a client-suggested task id by tenant."""
        return f"{tenant}::{task_id}"

    def _authenticate(self, delegation: dict[str, Any]) -> Caller:
        """Check identity, and build the caller this server will act for.

        Raises:
            AdmissionRefused: On a missing tenant or task id, a forwarded
                credential, an expired or wrongly-addressed delegation, or
                a delegation without the advertised scope.
        """
        tenant = str(delegation.get("tenant") or "")
        task_id = str(delegation.get("task_id") or "")
        if not tenant:
            raise AdmissionRefused(
                "delegation carries no tenant. A receiver must never infer "
                "the tenant from the credential it was called with."
            )
        if not task_id:
            raise AdmissionRefused(
                "delegation carries no task_id. The id anchors idempotency "
                "and must be derived by the caller from its run and step."
            )

        auth = delegation.get("auth")
        if not isinstance(auth, dict):
            raise AdmissionRefused("delegation carries no auth block")
        forwarded = sorted(RAW_TOKEN_KEYS & set(auth))
        if forwarded:
            raise AdmissionRefused(
                f"auth block carries {', '.join(forwarded)}: that is the "
                "caller's credential, not a delegation. Send a scoped, "
                "short-lived grant this server can exchange under its own "
                "identity."
            )
        if auth.get("kind") != "delegation":
            raise AdmissionRefused(
                f"auth.kind is {auth.get('kind')!r}; expected 'delegation'"
            )
        if auth.get("audience") != AUDIENCE:
            raise AdmissionRefused(
                f"delegation is addressed to {auth.get('audience')!r}, not "
                f"{AUDIENCE!r}"
            )
        if float(auth.get("expires_at", 0.0)) <= self._time():
            raise AdmissionRefused("delegation has expired")

        scopes = frozenset(str(s) for s in auth.get("scopes") or ())
        if REQUIRED_SCOPE not in scopes:
            raise AdmissionRefused(
                f"delegation lacks {REQUIRED_SCOPE!r}; it carries "
                f"{sorted(scopes) or '(nothing)'}"
            )
        return Caller(
            tenant=tenant,
            task_id=task_id,
            subject=str(auth.get("subject", "")),
            actor=str(auth.get("actor", "")),
            scopes=scopes,
            assurance=str(auth.get("assurance", "")),
            chain=tuple(str(a) for a in auth.get("chain") or ()),
        )

    def _decline(
        self,
        delegation: dict[str, Any],
        caller: Caller,
    ) -> str | None:
        """Why this payload gets a ``rejected`` task, or ``None`` to accept."""
        missing = [f for f in HANDOFF_FIELDS if f not in delegation]
        if missing:
            return (
                f"handoff contract incomplete: missing "
                f"{', '.join(missing)}. All six fields travel across a "
                "boundary; in-process they were merely good practice."
            )
        budget = delegation.get("budget_remaining")
        if not isinstance(budget, int) or isinstance(budget, bool):
            return (
                "budget_remaining must be an integer number of cents "
                "remaining, not a fresh allowance"
            )
        if len(self.open_tasks_for(caller.tenant)) >= MAX_OPEN_TASKS_PER_TENANT:
            return (
                f"tenant {caller.tenant} already holds "
                f"{MAX_OPEN_TASKS_PER_TENANT} open tasks"
            )
        return self.agent.declines(
            str(delegation.get("skill", "")), self._claim(delegation)
        )

    @staticmethod
    def _claim(delegation: dict[str, Any]) -> dict[str, Any]:
        """The claim block: what the peer needs, pulled out of the handoff.

        ``order_id`` comes from the state reference rather than being parsed
        out of the goal sentence, and the threshold comes from the restated
        constraints rather than from a constant on this side.
        """
        constraints = delegation.get("constraints") or {}
        state_ref = delegation.get("state_ref") or {}
        return {
            "order_id": str(state_ref.get("order_id", "")),
            "approval_threshold_cents": constraints.get(
                "approval_threshold_cents"
            ),
        }

    def _time(self) -> float:
        """The server's clock. Injected, so nothing in the suite sleeps."""
        if self._clock is not None:
            return float(self._clock())
        return self._now

    def _record(
        self,
        op: str,
        caller: Caller,
        outcome: str,
        state: str,
        detail: str = "",
    ) -> None:
        """Append one audit record naming both principals."""
        self.audit.append(
            {
                "op": op,
                "tenant": caller.tenant,
                "task_id": caller.task_id,
                "subject": caller.subject,
                "actor": caller.actor,
                "chain": list(caller.chain),
                "scopes": sorted(caller.scopes),
                "outcome": outcome,
                "state": state,
                "detail": detail,
            }
        )


def evidence_message(text: str = "") -> dict[str, Any]:
    """The message that answers ``input_required`` for this peer's skill."""
    return {
        "role": "user",
        "artifact": EVIDENCE_ARTIFACT,
        "text": text or f"attached: {EVIDENCE_ARTIFACT}",
    }


def step_up_message(assurance: str = REQUIRED_ASSURANCE) -> dict[str, Any]:
    """The message that answers ``auth_required`` for this peer's skill."""
    return {"role": "user", "assurance": assurance}
