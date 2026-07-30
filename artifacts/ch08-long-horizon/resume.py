"""The resume path, and the four checks it does in a deliberate order.

Version before fingerprint, fingerprint before decision, decision before
dispatch. Every one of those orderings is load-bearing.

*Version first*, because a checkpoint deserialised into code it was not
written for has been silently migrated by nobody, and every check after
that one would be reading the wrong shape.

*Fingerprint before the decision*, because the question "did a human
approve this call" is meaningless until you know which call you are holding.
A stale fingerprint returns the run to ``waiting_approval`` with a reason
rather than raising, which is the branch the opening incident did not have.

*Decision before dispatch*, obviously, and *resolution before dispatch*,
which is less obvious: an intent recorded with no outcome is a question, and
the answer is a read against the authoritative system rather than a repeat
of the call. With a derived key that read is available to any worker,
because the key is recomputed rather than loaded. That is the whole
argument, and :func:`resume` is where it stops being an argument.

The key is computed here, never read back. A generated key would have had to
survive in storage; a derived one only has to survive in the two identifiers
the checkpoint already holds.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from approvals import PendingApproval
from envelope import ConfigDrift, Envelope
from keys import key_for
from migrate import migrate as run_migration
from migrate import plan
from northstar_contracts import RunState, ToolCall
from northstar_policy import approval_fingerprint
from pause import APPROVAL_QUEUE, Workflow
from states import RunPhase

__all__ = [
    "RESUME_CHECKS",
    "ResumeOutcome",
    "argument_diff",
    "fingerprint_of",
    "replay_prior_steps",
    "resume",
    "with_key",
]

#: The checks a resume runs, in the order it runs them. The order is the
#: design; each entry is a state the run can legitimately end up in.
RESUME_CHECKS: tuple[str, ...] = (
    "version",
    "fingerprint",
    "decision",
    "prior_steps",
    "dispatch",
)


@dataclass
class ResumeOutcome:
    """What a resume did, and why.

    Attributes:
        envelope: The run, after the resume. Its ``phase`` is the answer to
            "where is this run now".
        outcome: One of ``dispatched``, ``stale_fingerprint``, ``rejected``,
            ``undecided``, or ``refused_version``.
        version_plan: ``pin``, ``migrate``, or ``refuse``.
        reason: Human-readable, and the same string recorded on the
            transition.
        resolved: Keys whose intent had no outcome and which were settled by
            an independent read rather than by repeating the call.
        replayed: One entry per step the resume re-presented before the
            pause. ``duplicate: False`` in any of them means an effect
            happened a second time.
        diff: On a stale fingerprint, what changed in the pending call's
            arguments. An approver re-asked without a diff is an approver
            being asked to guess.
    """

    envelope: Envelope
    outcome: str
    version_plan: str
    reason: str = ""
    resolved: list[str] = field(default_factory=list)
    replayed: list[dict[str, Any]] = field(default_factory=list)
    diff: dict[str, Any] = field(default_factory=dict)

    @property
    def state(self) -> RunState:
        """The run state, which is what the chapter's excerpt returns."""
        return self.envelope.state

    @property
    def phase(self) -> RunPhase:
        """The run's phase after the resume."""
        return self.envelope.phase


def fingerprint_of(call: ToolCall, run_id: str) -> str:
    """Fingerprint one call, through the package and not a second copy.

    Two canonicalizers in one repository is one more than any system can
    afford, and the second one is always the one that drifts.
    """
    return approval_fingerprint(call, run_id)


def with_key(call: ToolCall, key: str) -> ToolCall:
    """The parked call, plus the idempotency key this attempt will present."""
    return ToolCall(
        id=call.id,
        name=call.name,
        arguments={**call.arguments, "idempotency_key": key},
    )


def argument_diff(
    approved: dict[str, Any],
    current: dict[str, Any],
) -> dict[str, Any]:
    """What changed between the approved arguments and the current ones.

    Reported per field rather than as a fingerprint mismatch, because
    "the hash differs" tells an approver nothing they can act on.
    """
    changes: dict[str, Any] = {}
    for field_name in sorted(set(approved) | set(current)):
        was = approved.get(field_name)
        now = current.get(field_name)
        if was != now:
            changes[field_name] = {"approved": was, "now": now}
    return changes


def resume(
    run_id: str,
    code_version: str,
    workflow: Workflow,
) -> ResumeOutcome:
    """Rehydrate a parked run on a worker that did not start it.

    Args:
        run_id: The run to continue.
        code_version: The agent version *this* worker is. Compared against
            the checkpoint before anything else happens.
        workflow: The collaborators -- envelope store, approval inbox,
            intent journal, refund service, policy, principal. The chapter's
            excerpt reads these from module scope; they are passed here
            because a module-level store is a store you cannot test twice.

    Returns:
        A :class:`ResumeOutcome`. Four of its five outcomes are ordinary
        states a run lives in, and only ``refused_version`` is an error.

    Raises:
        ConfigDrift: When the stored version has no declared path to this
            worker's version. Pin, migrate, or cancel -- never guess.
        UnknownRun: When nothing was ever checkpointed under ``run_id``.
    """
    store = workflow.store
    envelope = store.load_envelope(run_id)
    actor = f"worker:{code_version}"
    rehydrating = envelope.phase is not RunPhase.RUNNING
    if rehydrating:
        store.transition(
            envelope, RunPhase.RESUMING, actor=actor, reason="lease taken"
        )
    else:
        # The previous worker died holding the lease. Lease expiry returns
        # the run to the queue, and that pair of transitions is how a run
        # survives a worker terminated between heartbeats. It is not a
        # rehydrate: the run never parked, so there is no step in front of
        # the pause to replay.
        store.transition(
            envelope,
            RunPhase.QUEUED,
            actor="lease-monitor",
            reason="lease expired",
        )
        store.transition(
            envelope, RunPhase.RUNNING, actor=actor, reason="lease taken"
        )

    # ---- 1. version ------------------------------------------------------
    version_plan = plan(envelope.agent_version, code_version)
    approved_arguments = dict(
        (envelope.pending_call or {}).get("arguments") or {}
    )
    if version_plan == "refuse":
        stored = envelope.agent_version
        store.transition(
            envelope,
            RunPhase.FAILED,
            actor=actor,
            reason=f"no migration from {stored} to {code_version}",
        )
        raise ConfigDrift(run_id, stored, code_version)
    if version_plan == "migrate":
        envelope = run_migration(envelope, to=code_version)
        envelope.config_hash = workflow.config.hash
        store.save(envelope)

    request = workflow.approvals.pending(run_id)
    if request is None:
        store.transition(
            envelope,
            RunPhase.FAILED,
            actor=actor,
            reason="no approval request for a parked run",
        )
        return ResumeOutcome(
            envelope, "rejected", version_plan, "no approval request"
        )

    # ---- 2. fingerprint --------------------------------------------------
    call = _pending_call(envelope, request)
    current = fingerprint_of(call, run_id)
    if current != request.fingerprint:
        diff = argument_diff(approved_arguments, call.arguments)
        reopened = workflow.approvals.request(
            run_id=run_id,
            step_id=request.step_id,
            call=call,
            reason=(
                "the call changed after it was approved: "
                + ", ".join(sorted(diff))
            ),
            principal=workflow.principal,
            queue=APPROVAL_QUEUE,
        )
        store.transition(
            envelope,
            RunPhase.WAITING_APPROVAL,
            actor=actor,
            reason=f"stale_fingerprint; re-requested {reopened.id}",
        )
        return ResumeOutcome(
            envelope,
            "stale_fingerprint",
            version_plan,
            reason="the approved call is not the call we are holding",
            diff=diff,
        )

    # ---- 3. decision -----------------------------------------------------
    if not request.decided:
        store.transition(
            envelope,
            RunPhase.WAITING_APPROVAL,
            actor=actor,
            reason="no decision yet",
        )
        return ResumeOutcome(
            envelope, "undecided", version_plan, "still waiting on a human"
        )
    if not request.approved:
        store.transition(
            envelope,
            RunPhase.FAILED,
            actor=actor,
            reason=f"rejected by {request.decided_by}",
        )
        return ResumeOutcome(
            envelope,
            "rejected",
            version_plan,
            reason=f"rejected by {request.decided_by}",
        )

    # ---- 4. prior steps: replay them, then resolve what is open ----------
    replayed = (
        replay_prior_steps(workflow, run_id, envelope.state.step)
        if rehydrating
        else []
    )
    resolved = _resolve_open_intents(workflow, run_id)

    # ---- 5. dispatch -----------------------------------------------------
    if envelope.phase is not RunPhase.RUNNING:
        store.transition(
            envelope,
            RunPhase.RUNNING,
            actor=actor,
            reason=f"approved by {request.decided_by}",
        )
    key = key_for(
        run_id, envelope.state.step, workflow.config.key_strategy
    )
    settled = workflow.ledger.intent(key)
    prior = workflow.service.lookup(key)
    if settled is not None and not settled.unresolved:
        # Already resolved a moment ago, by check 4. Nothing to dispatch and
        # nothing to report twice.
        envelope.pending_call = None
        envelope.state = envelope.state.advance()
        store.save(envelope)
    elif prior is not None:
        # Resolve, do not repeat. The pre-crash attempt committed under this
        # key; the read settles it without a second payout. Only a worker
        # that can *recompute* the key gets this branch.
        workflow.ledger.record_intent(
            key=key,
            run_id=run_id,
            step_id=envelope.state.step,
            tool=call.name,
            arguments=call.arguments,
            at=float(envelope.state.step),
        )
        workflow.ledger.record_outcome(key, {**prior, "duplicate": True})
        resolved.append(key)
        envelope.pending_call = None
        envelope.state = envelope.state.advance()
        store.save(envelope)
    else:
        envelope = workflow.dispatch(envelope, with_key(call, key), key)

    store.transition(
        envelope,
        RunPhase.SUCCEEDED,
        actor=actor,
        reason="refund settled once",
    )
    return ResumeOutcome(
        envelope,
        "dispatched",
        version_plan,
        reason=f"approved by {request.decided_by}",
        resolved=resolved,
        replayed=replayed,
    )


def replay_prior_steps(
    workflow: Workflow,
    run_id: str,
    before_step: int,
) -> list[dict[str, Any]]:
    """Re-present every step the run took before the pause.

    This is the part of the opening incident nobody designed. A resume
    re-executes the step in front of the pause -- here a ``send_message``
    that already went out on Friday -- and whether the customer reads it a
    second time depends entirely on which key the resumed run presents.

    With a derived key the key is recomputed from ``(run_id, step_id)``, the
    target recognises it, and the reply is the original receipt. With a
    nonce the target sees a new intent, and the notice goes out again three
    days later.

    Returns:
        One entry per prior step: its id, tool, key, and whether the target
        recognised it. A ``duplicate`` of ``False`` here means an effect
        happened twice.
    """
    seen: set[int] = set()
    replayed: list[dict[str, Any]] = []
    for intent in workflow.ledger.intents(run_id):
        if intent.step_id >= before_step or intent.step_id in seen:
            continue
        seen.add(intent.step_id)
        key = key_for(run_id, intent.step_id, workflow.config.key_strategy)
        call = ToolCall(
            id=f"replay-{intent.step_id}",
            name=intent.tool,
            arguments=dict(intent.arguments),
        )
        outcome = workflow.present(call, key)
        replayed.append(
            {
                "step_id": intent.step_id,
                "tool": intent.tool,
                "key": key,
                "duplicate": bool(outcome.get("duplicate")),
            }
        )
    return replayed


def _pending_call(
    envelope: Envelope,
    request: PendingApproval,
) -> ToolCall:
    """The call the run is parked in front of.

    Read from the envelope when it is there, because that is what a
    migration rewrites. Falling back to the request's own copy would hide
    exactly the change this path exists to detect.
    """
    if envelope.pending_call is not None:
        return ToolCall.from_dict(envelope.pending_call)
    return request.call


def _resolve_open_intents(workflow: Workflow, run_id: str) -> list[str]:
    """Settle every intent with no outcome, by reading rather than repeating.

    An intent whose key the refund service knows is settled from the
    service's own record. An intent whose key it does not know never
    committed, so there is nothing to resolve and the ordinary path will
    issue it. Neither branch repeats a call blindly, which is the rule.
    """
    resolved: list[str] = []
    for intent in workflow.ledger.unresolved(run_id):
        prior = workflow.service.lookup(intent.key)
        if prior is None:
            continue
        workflow.ledger.record_outcome(
            intent.key, {**prior, "duplicate": True}
        )
        resolved.append(intent.key)
    return resolved
