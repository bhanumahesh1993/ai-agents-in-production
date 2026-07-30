"""The fraud review agent, on its own runtime.

The support agent runs on the book's ``AgentLoop``. This one does not, on
purpose: it is a small explicit node graph, the shape a graph runtime such
as LangGraph or a step-function workflow gives you. Neither side can see
the other's runtime, and neither has to, which is the property A2A is for.

The graph has five nodes and two of them stop. ``check_assurance`` stops in
``auth_required`` when the delegation's credential is too weak for a claim
this size. ``need_evidence`` stops in ``input_required`` when the agent
concludes it cannot clear the claim without a photograph of the shipping
label. That second stop is the 08:50 moment from the chapter's incident:
the work is neither progressing nor failing, and the row in Northstar's old
Postgres table said ``pending`` for both.

The agent holds its own :class:`~northstar_contracts.world.World`. Northstar
has one system of record, but this agent reads it through its own
deployment, so the artifact does not quietly hand the two agents a shared
object and call it a protocol boundary.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from northstar_contracts import Money, World, canonical_json
from wire import (
    APPROVAL_THRESHOLD_CENTS,
    TASK_STATE_AUTH_REQUIRED,
    TASK_STATE_COMPLETED,
    TASK_STATE_FAILED,
    TASK_STATE_INPUT_REQUIRED,
    TASK_STATE_WORKING,
    IllegalTransition,
    Task,
)

__all__ = [
    "CARD_PATH",
    "EVIDENCE_ARTIFACT",
    "EVIDENCE_THRESHOLD_CENTS",
    "FRAUD_REVIEW_SKILL",
    "REQUIRED_ASSURANCE",
    "STEP_UP_SCOPE",
    "FraudReviewAgent",
    "Verdict",
    "published_card",
    "sign_card",
]

HERE = Path(__file__).resolve().parent
CARD_DIR = HERE.parent / "cards"

#: The card this peer publishes at ``/.well-known/agent-card.json``.
CARD_PATH = CARD_DIR / "fraud-review.json"

#: The detached signature served alongside it.
SIG_PATH = CARD_DIR / "fraud-review.sig.json"

#: A pre-1.0 card, kept as the porting counter-example. It carries a
#: top-level ``url``, ``protocolVersion``, and ``preferredTransport``, and
#: the client refuses it rather than reading around the missing interface
#: list.
PRE_1_0_CARD_PATH = CARD_DIR / "fraud-review.pre-1.0.json"

#: Signing key for the card. Not a secret, and not asymmetric: a real
#: deployment publishes a detached JWS and the client verifies it against a
#: public key it pinned. An HMAC stand-in keeps this artifact free of a
#: crypto dependency while demonstrating the same property, which is that a
#: card body which changed by one byte stops verifying.
CARD_SIGNING_KEY = b"ch10-mock-card-signing-key-not-a-secret"

#: The one skill this agent advertises.
FRAUD_REVIEW_SKILL = "assess_claim"

#: Claims at or above this need a photograph of the shipping label, and a
#: step-up credential. Below it the agent decides on signals alone.
EVIDENCE_THRESHOLD_CENTS: Money = 20000

#: The artifact the caller must supply to leave ``input_required``.
EVIDENCE_ARTIFACT = "shipping_label_photo"

#: Assurance level the delegation must carry for a claim over the evidence
#: threshold. Below that the agent stops in ``auth_required``, which routes
#: to an authorization server rather than to a person.
REQUIRED_ASSURANCE = "mfa"

#: Scope the caller must step up to obtain.
STEP_UP_SCOPE = "fraud.review.decide"


def sign_card(body: dict[str, Any]) -> str:
    """Sign a card body over its canonical JSON.

    Args:
        body: The card document, as :meth:`wire.AgentCard.to_dict` renders
            it. Canonicalising first is what makes the signature stable
            across key order and whitespace.

    Returns:
        A hex digest.
    """
    payload = canonical_json(body).encode("utf-8")
    return hmac.new(CARD_SIGNING_KEY, payload, hashlib.sha256).hexdigest()


def published_card() -> tuple[dict[str, Any], str]:
    """The card body and its detached signature, as the peer serves them.

    Returns:
        ``(body, signature)``. The signature is read from disk rather than
        recomputed, so an edit to the card that nobody re-signed is caught
        by verification instead of being papered over at serve time.
    """
    body = json.loads(CARD_PATH.read_text())
    signature = str(json.loads(SIG_PATH.read_text())["signature"])
    return body, signature


def pre_1_0_card() -> dict[str, Any]:
    """The legacy-shaped card, for the porting test and the demo."""
    return dict(json.loads(PRE_1_0_CARD_PATH.read_text()))


@dataclass(frozen=True)
class Verdict:
    """What the fraud review agent concluded. The task's only artifact.

    ``requires_approval`` is computed against the threshold that arrived in
    the delegation's ``constraints``, not against a constant this agent
    holds. A restated constraint that the receiver ignores is decoration.
    """

    order_id: str
    claim_cents: Money
    risk: str
    decision: str
    requires_approval: bool
    approval_threshold_cents: Money
    signals: tuple[str, ...]
    evidence: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        """JSON-serialisable form, for the task artifact."""
        return {
            "order_id": self.order_id,
            "claim_cents": self.claim_cents,
            "risk": self.risk,
            "decision": self.decision,
            "requires_approval": self.requires_approval,
            "approval_threshold_cents": self.approval_threshold_cents,
            "signals": list(self.signals),
            "evidence": list(self.evidence),
        }


@dataclass
class _Memory:
    """The agent's private working state for one task.

    Deliberately not on the :class:`wire.Task`. The task is what crosses the
    boundary; this is what the peer knows and the caller does not, and
    keeping them in separate objects is what stops internal state leaking
    into the wire format by accident.
    """

    node: str = "start"
    claim_cents: Money = 0
    order_id: str = ""
    signals: list[str] = field(default_factory=list)
    evidence: list[str] = field(default_factory=list)
    assurance: str = ""
    threshold_cents: Money = APPROVAL_THRESHOLD_CENTS


class FraudReviewAgent:
    """The peer's reasoning, as a node graph with two blocking stops.

    Args:
        world: The agent's own view of Northstar's orders. Defaults to a
            fresh :class:`~northstar_contracts.world.World`, which is what
            makes this agent's fixtures independent of the caller's.

    Attributes:
        reviews_opened: How many fraud reviews were actually started. The
            number the idempotency property is about: a resent delegation
            must not increase it.
        checks_run: How many times the signal checks ran. A resumed task
            re-enters the graph; it does not re-run work it already did.
    """

    def __init__(self, world: World | None = None) -> None:
        self.world = world or World()
        self.reviews_opened = 0
        self.checks_run = 0
        self._memory: dict[str, _Memory] = {}

    # ------------------------------------------------------------ admission

    def declines(self, skill: str, claim: dict[str, Any]) -> str | None:
        """Why this agent would refuse the request, or ``None`` to accept.

        Admission, not work. Everything checked here is cheap and reads
        nothing but the request, which is what makes a rejected task
        genuinely different from a failed one: no domain work ran, so
        resending the identical payload will be refused identically.

        Args:
            skill: The skill the caller invoked.
            claim: The delegation's claim block.

        Returns:
            A reason string, or ``None``.
        """
        if skill != FRAUD_REVIEW_SKILL:
            return (
                f"skill {skill!r} is not offered; this agent offers "
                f"{FRAUD_REVIEW_SKILL!r}"
            )
        order_id = str(claim.get("order_id", ""))
        if not order_id:
            return "claim is missing order_id"
        if order_id not in self.world.orders:
            return f"order {order_id!r} is not in this agent's system of record"
        threshold = claim.get("approval_threshold_cents")
        if not isinstance(threshold, int) or isinstance(threshold, bool):
            return (
                "constraints.approval_threshold_cents did not travel with "
                "the delegation, so this agent cannot enforce the caller's "
                "approval rule"
            )
        return None

    # ---------------------------------------------------------------- graph

    def open(self, task: Task, claim: dict[str, Any], assurance: str) -> Task:
        """Accept an admitted task. It stays ``submitted`` until it runs.

        Splitting acceptance from execution is not ceremony. A ``submitted``
        task may not have touched anything yet; a ``working`` task may
        already have side effects. Collapsing them, as Northstar's
        ``pending`` did, throws away the only signal that distinguishes a
        safe retry from a dangerous one.

        Args:
            task: A task in ``submitted``.
            claim: The delegation's claim block.
            assurance: Assurance level the caller's delegation carried.

        Returns:
            The task, still in ``submitted``.
        """
        self._memory[task.id] = _Memory(
            order_id=str(claim["order_id"]),
            assurance=assurance,
            threshold_cents=int(claim["approval_threshold_cents"]),
        )
        self.reviews_opened += 1
        return task

    def supply_evidence(self, task: Task, name: str) -> Task:
        """Hand the agent what it asked for, and let it continue.

        Args:
            task: A task in ``input_required``.
            name: The artifact the caller is supplying.

        Returns:
            The task, back in ``working``.

        Raises:
            IllegalTransition: If the task is not waiting on the caller.
        """
        if task.state != TASK_STATE_INPUT_REQUIRED:
            raise IllegalTransition(
                f"task {task.id} is not waiting on input; it is "
                f"{task.state}."
            )
        memory = self._memory[task.id]
        memory.evidence.append(name)
        memory.node = "decide"
        task.move_to(TASK_STATE_WORKING, note=f"received {name}")
        return task

    def step_up(self, task: Task, assurance: str) -> Task:
        """Present a stronger credential and let the agent continue.

        Args:
            task: A task in ``auth_required``.
            assurance: The new assurance level.

        Returns:
            The task, back in ``working``.

        Raises:
            IllegalTransition: If the task is not waiting on authorization.
        """
        if task.state != TASK_STATE_AUTH_REQUIRED:
            raise IllegalTransition(
                f"task {task.id} is not waiting on authorization; it is "
                f"{task.state}."
            )
        memory = self._memory[task.id]
        memory.assurance = assurance
        memory.node = "score_signals"
        task.required_scopes = []
        task.move_to(TASK_STATE_WORKING, note="step-up credential accepted")
        return task

    def tick(self, task: Task) -> Task:
        """Run at most one graph node. The peer making progress.

        One node per call, rather than running to a stop, because that is
        what a long-running peer looks like from outside: the caller reads
        the task and sees it further along. It is also what makes the
        client's poll loop worth writing, since a peer that finished before
        it replied would never show a client the difference between waiting
        on work and waiting on a person.

        A task that is terminal, or blocked in ``input_required`` or
        ``auth_required``, is left exactly as it is. There is no clock and
        no sleep here: waiting costs the peer a row, not a thread.

        Returns:
            The task.
        """
        memory = self._memory.get(task.id)
        if memory is None or task.is_terminal or not memory.node:
            return task
        if task.state in (TASK_STATE_INPUT_REQUIRED, TASK_STATE_AUTH_REQUIRED):
            return task
        nodes: dict[str, Callable[[Task, _Memory], str]] = {
            "start": self._start,
            "read_order": self._read_order,
            "check_assurance": self._check_assurance,
            "score_signals": self._score_signals,
            "need_evidence": self._need_evidence,
            "decide": self._decide,
        }
        memory.node = nodes[memory.node](task, memory)
        return task

    def _start(self, task: Task, memory: _Memory) -> str:
        """``submitted -> working``: execution has begun."""
        task.move_to(TASK_STATE_WORKING, note="fraud review started")
        return "read_order"

    def _read_order(self, task: Task, memory: _Memory) -> str:
        """Read the claim's order out of this agent's own world."""
        order = self.world.get_order(memory.order_id)
        memory.claim_cents = int(order["refundable_cents"])
        task.add_message(
            "agent",
            f"read {memory.order_id}: {order['status']}, "
            f"{memory.claim_cents} cents refundable",
        )
        return "check_assurance"

    def _check_assurance(self, task: Task, memory: _Memory) -> str:
        """Stop in ``auth_required`` when the credential is too weak.

        Separate from :meth:`_need_evidence` because the resolution path is
        different: this one routes to an authorization server, that one to
        a person. A client that merges them asks a customer for an OAuth
        grant.
        """
        if (
            memory.claim_cents >= EVIDENCE_THRESHOLD_CENTS
            and memory.assurance != REQUIRED_ASSURANCE
        ):
            task.required_scopes = [STEP_UP_SCOPE]
            task.move_to(
                TASK_STATE_AUTH_REQUIRED,
                note=(
                    f"a claim of {memory.claim_cents} cents needs a "
                    f"{REQUIRED_ASSURANCE} credential and the "
                    f"{STEP_UP_SCOPE} scope"
                ),
            )
            return ""
        return "score_signals"

    def _score_signals(self, task: Task, memory: _Memory) -> str:
        """Run the checks. Deterministic, and counted so tests can see it."""
        self.checks_run += 1
        order = self.world.orders[memory.order_id]
        if "fraud_review" in order["flags"]:
            memory.signals.append("order_flagged_fraud_review")
        if order["status"] != "delivered":
            memory.signals.append(f"not_delivered_status_{order['status']}")
        if memory.claim_cents >= EVIDENCE_THRESHOLD_CENTS:
            memory.signals.append("claim_over_evidence_threshold")
        task.add_message(
            "agent", f"{len(memory.signals)} signal(s): {memory.signals}"
        )
        return "need_evidence"

    def _need_evidence(self, task: Task, memory: _Memory) -> str:
        """Stop in ``input_required`` when a person has to send something.

        The 08:50 state. Not progressing, not failing, and legitimately
        able to sit for hours while somebody finds a photograph. The
        message names exactly what is needed, because a client that has to
        guess cannot ask the customer anything useful.
        """
        needs_photo = (
            memory.claim_cents >= EVIDENCE_THRESHOLD_CENTS
            and EVIDENCE_ARTIFACT not in memory.evidence
        )
        if needs_photo:
            task.move_to(
                TASK_STATE_INPUT_REQUIRED,
                note=(
                    f"Send a {EVIDENCE_ARTIFACT} for {memory.order_id} "
                    f"before this {memory.claim_cents}-cent claim can be "
                    f"cleared."
                ),
            )
            return ""
        return "decide"

    def _decide(self, task: Task, memory: _Memory) -> str:
        """Produce the verdict and complete the task."""
        risk = "elevated" if len(memory.signals) >= 2 else "ordinary"
        cleared = EVIDENCE_ARTIFACT in memory.evidence or risk == "ordinary"
        verdict = Verdict(
            order_id=memory.order_id,
            claim_cents=memory.claim_cents,
            risk=risk,
            decision="clear" if cleared else "hold",
            requires_approval=memory.claim_cents >= memory.threshold_cents,
            approval_threshold_cents=memory.threshold_cents,
            signals=tuple(memory.signals),
            evidence=tuple(memory.evidence),
        )
        task.add_artifact("fraud_verdict", verdict.to_dict())
        task.move_to(
            TASK_STATE_COMPLETED,
            note=f"{verdict.decision}: risk {verdict.risk}",
        )
        return ""

    def fail(self, task: Task, reason: str) -> Task:
        """Move a started task to ``failed``. Partial state may remain."""
        task.move_to(TASK_STATE_FAILED, note=reason)
        return task

    def verdict_of(self, task: Task) -> dict[str, Any] | None:
        """The verdict artifact, or ``None`` if the task produced none."""
        for artifact in task.artifacts:
            if artifact["name"] == "fraud_verdict":
                return dict(artifact["content"])
        return None
