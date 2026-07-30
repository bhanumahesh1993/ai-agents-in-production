"""Preview, commit, and the side-effect ledger.

Everything in ``specs.py`` is about the model reading the contract correctly.
This module is about what happens when it reads it correctly and the world
still ends up wrong, which is the class Chapter 1's double refund belongs to.
The repair at the tool layer has three parts.

**A preview that commits nothing.** :func:`preview_refund` takes the same
arguments as :func:`issue_refund` and returns exactly what would happen. It is
a separate tool with ``writes=False``, not a flag, which is what lets a
read-only agent run it, lets a policy engine allow it unconditionally, and
lets an approval payload be built from its output.

**A commit that carries a derived key.** The key comes from the run and the
step, never from a fresh generator per attempt. A key created on each retry is
a nonce, and it defeats the mechanism precisely when it is needed.

**A ledger that records intent before the external call and outcome after
it.** The write *before* the call is the part that is easy to skip and
expensive to omit: if the process dies between the external call and the
response, the only record that the intent existed is the one written
beforehand. A ledger that records only successes cannot tell "we never tried"
from "we tried and do not know", and those two states need different recovery.

The ledger is a separate store from conversational memory and from the run's
event log, for a simple reason: it must survive the deletion of the
conversation, and it is the artifact an auditor reads.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from northstar_contracts import (
    REFUND_APPROVAL_THRESHOLD_CENTS,
    Money,
    ToolTimeout,
    ToolValidationError,
    World,
    content_hash,
)
from northstar_policy import Principal
from specs import Compensation, compensation_for

__all__ = [
    "POLICY_REASON",
    "LedgerRow",
    "RefundPath",
    "SideEffectLedger",
    "cancel_refund",
    "issue_refund",
    "preview_refund",
]

#: The tool's ``reason`` vocabulary is the caller's; the policy store's is the
#: store's. They are not the same list and pretending otherwise is how a
#: reason ends up as free text. The tool is where the two meet, and this map
#: is the whole translation -- kept here, next to the call, rather than in the
#: schema, so that widening the tool's enum is a schema change and remapping
#: is a code change.
POLICY_REASON: dict[str, str] = {
    "damaged": "damaged",
    "not_received": "not_delivered",
    "wrong_item": "damaged",
    "changed_mind": "changed_mind",
}


@dataclass
class LedgerRow:
    """One logical intent, and what became of it.

    Args:
        key: The idempotency key. One row per key, never one row per attempt.
        tool: Which tool was called.
        version: The tool version that was pinned when the run started. A
            resumed run uses its pinned version, not today's.
        args_fingerprint: Canonical hash of the arguments, so an auditor can
            tell two intents apart without the ledger holding the payload.
        principal: Who the call acted as.
        outcome: ``"pending"`` until the call returns, then ``"settled"``,
            ``"duplicate"``, or ``"unknown"``.
        receipt: The receipt, once there is one.
        compensation: The declared inverse, if the tool has one.
    """

    key: str
    tool: str
    version: str
    args_fingerprint: str
    principal: str
    outcome: str = "pending"
    receipt: dict[str, Any] | None = None
    compensation: str = ""

    def to_dict(self) -> dict[str, Any]:
        """JSON-serialisable form. This is what an auditor is handed."""
        return {
            "key": self.key,
            "tool": self.tool,
            "version": self.version,
            "args_fingerprint": self.args_fingerprint,
            "principal": self.principal,
            "outcome": self.outcome,
            "receipt": dict(self.receipt) if self.receipt else None,
            "compensation": self.compensation,
        }


@dataclass
class SideEffectLedger:
    """One row per logical intent, written before the effect and after it.

    Attributes:
        rows: In insertion order.
        principal: Who the ledger attributes calls to.
    """

    rows: list[LedgerRow] = field(default_factory=list)
    principal: Principal = field(
        default_factory=lambda: Principal.of(
            "CUST-8841", "refunds:write", agent_id="northstar-support-agent"
        )
    )

    def record_intent(
        self,
        *,
        key: str,
        tool: str,
        version: str = "1",
        **arguments: Any,
    ) -> LedgerRow:
        """Write the intent down *before* the external call.

        Returns the existing row when the key has been seen, because a retry
        of the same logical intent is the same row. Two rows for one key would
        make the ledger say the customer was charged twice by a mechanism
        built to guarantee they were not.
        """
        existing = self.find(key)
        if existing is not None:
            return existing
        compensation: Compensation | None = compensation_for(tool)
        row = LedgerRow(
            key=key,
            tool=tool,
            version=version,
            args_fingerprint=content_hash(arguments)[:16],
            principal=self.principal.agent_id,
            compensation=compensation.tool if compensation else "",
        )
        self.rows.append(row)
        return row

    def record_outcome(
        self,
        *,
        key: str,
        receipt: dict[str, Any],
    ) -> LedgerRow:
        """Write what happened, once the call has returned."""
        row = self._require(key)
        row.receipt = dict(receipt)
        row.outcome = str(receipt.get("status", "settled"))
        return row

    def record_unknown(self, *, key: str) -> LedgerRow:
        """The call timed out. Whether the effect landed is not known.

        This is the row a reconciliation loop reads: for every intent with no
        outcome, query the target system by key and write down what actually
        happened. Chapter 21 builds that loop; the tool's job is to leave
        behind enough evidence for it to run.
        """
        row = self._require(key)
        row.outcome = "unknown"
        return row

    def find(self, key: str) -> LedgerRow | None:
        """The row for one key, or ``None``."""
        for row in self.rows:
            if row.key == key:
                return row
        return None

    def unresolved(self) -> list[LedgerRow]:
        """Intents with no settled outcome. What reconciliation works on."""
        return [r for r in self.rows if r.outcome in ("pending", "unknown")]

    def receipts(self) -> list[dict[str, Any]]:
        """Every receipt the ledger holds."""
        return [dict(r.receipt) for r in self.rows if r.receipt]

    def irreversible(self) -> list[LedgerRow]:
        """Rows whose tool has no declared inverse at all."""
        return [r for r in self.rows if not r.compensation]

    def _require(self, key: str) -> LedgerRow:
        row = self.find(key)
        if row is None:
            raise KeyError(
                f"no intent recorded for {key!r}. record_intent must run "
                "before the external call, not after it."
            )
        return row

    def to_dicts(self) -> list[dict[str, Any]]:
        """The whole ledger, JSON-serialisable."""
        return [r.to_dict() for r in self.rows]


# ------------------------------------------------------------------ the tools


def preview_refund(
    order_id: str,
    amount_cents: Money,
    reason: str,
    *,
    world: World,
) -> dict[str, Any]:
    """What issuing this refund would do. A read, so allow it freely.

    A dry run against the world, never the model's account of the world. The
    output is what an approval payload is built from, which is the practical
    reason this is a tool rather than a flag: a flag on the mutating tool
    leaves the call registered as a write and authorized as one.

    Args:
        order_id: The order.
        amount_cents: Integer cents.
        reason: One of :data:`specs.REFUND_REASONS`.
        world: The system of record.

    Returns:
        The amount, the refundable balance, the policy clauses that permit it,
        the status the order would end in, whether it would need human
        approval, and how long it would stay reversible.

    Raises:
        ToolValidationError: If the order is unknown, or the amount exceeds
            what is refundable. The message names the amount that would work,
            because the next turn should be a corrected call rather than a
            guess.
    """
    order = world.get_order(order_id)
    refundable = int(order["refundable_cents"])
    if amount_cents > refundable:
        raise ToolValidationError(
            f"amount_exceeds_order_total: {amount_cents} exceeds the "
            f"refundable total {refundable} for order {order_id}. Refund at "
            f"most {refundable}, or call escalate_to_specialist for a "
            f"goodwill exception."
        )
    policy = world.get_policy(reason=POLICY_REASON[reason])
    threshold = int(policy["approval_threshold_cents"])
    compensation = compensation_for("issue_refund")
    remaining = refundable - amount_cents
    return {
        "order_id": order_id,
        "amount_cents": amount_cents,
        "refundable_cents": refundable,
        "policy_clauses": [
            f"{r['reason']}: eligible={r['eligible']}, "
            f"refund_pct={r['refund_pct']}, window={r['window_days']}d"
            for r in policy["rules"]
        ],
        "resulting_status": "refunded" if remaining == 0 else "part_refunded",
        "requires_approval": amount_cents >= threshold,
        "reversible_for": compensation.window if compensation else "not at all",
    }


def issue_refund(
    order_id: str,
    amount_cents: Money,
    reason: str,
    idempotency_key: str,
    *,
    world: World,
    ledger: SideEffectLedger,
    version: str = "3",
) -> dict[str, Any]:
    """Idempotent: same key returns the original receipt.

    Args:
        order_id: The order.
        amount_cents: Integer cents.
        reason: One of :data:`specs.REFUND_REASONS`.
        idempotency_key: Derived from the run id and the step id. Required by
            the schema, because a tool that claims deduplication it cannot
            perform is the most dangerous lie a contract can tell.
        world: The system of record.
        ledger: Where intent is recorded before the call.
        version: The pinned tool version, recorded on the ledger row.

    Returns:
        ``{"receipt_id", "amount_cents", "status"}``, and nothing else. The
        world returns more than that; shaping to the declared output schema is
        what keeps the receipt inside its 200-token budget.

    Raises:
        ToolTimeout: When the outcome is unknown. The ledger row is marked
            ``unknown`` first, and the caller must reconcile rather than retry
            blind.
    """
    # Principal: support agent, scope refunds.write, TTL 60s.
    ledger.record_intent(
        key=idempotency_key,
        tool="issue_refund",
        version=version,
        order_id=order_id,
        amount=amount_cents,
    )
    try:
        receipt = world.issue_refund(
            order_id,
            amount_cents,
            POLICY_REASON[reason],
            idempotency_key=idempotency_key,
        )
    except ToolTimeout:
        # Outcome unknown. Do not retry blind: reconcile.
        ledger.record_unknown(key=idempotency_key)
        raise
    shaped = {
        "receipt_id": str(receipt["refund_id"]),
        "amount_cents": int(receipt["amount_cents"]),
        "status": "duplicate" if receipt.get("duplicate") else "settled",
    }
    ledger.record_outcome(key=idempotency_key, receipt=shaped)
    return shaped


def cancel_refund(
    receipt_id: str,
    *,
    ledger: SideEffectLedger,
) -> dict[str, Any]:
    """The declared inverse of ``issue_refund``, within its window.

    Not registered as a tool the model may call. It exists so that a human
    reviewing an approval can be told the action is reversible, and so a
    supervisor can undo a bad step without inventing a procedure. Compensation
    is an operator capability, not an agent capability.

    Args:
        receipt_id: The receipt to reverse.
        ledger: The side-effect ledger.

    Returns:
        The compensation record.

    Raises:
        KeyError: If no ledger row holds that receipt.
    """
    for row in ledger.rows:
        if row.receipt and row.receipt.get("receipt_id") == receipt_id:
            row.outcome = "compensated"
            compensation = compensation_for(row.tool)
            return {
                "receipt_id": receipt_id,
                "compensated_by": row.compensation,
                "window": compensation.window if compensation else "",
            }
    raise KeyError(f"no ledger row holds receipt {receipt_id!r}")


@dataclass
class RefundPath:
    """The refund path with its world and ledger bound.

    The free functions above take ``world`` and ``ledger`` as keyword
    arguments, which is how they read in the chapter. A
    :class:`~northstar_runtime.registry.ToolRegistry` calls a tool with the
    model's arguments and nothing else, so registration needs them bound --
    and binding them here rather than reaching for module-level globals is
    what lets one test build a world, a ledger, and a path of its own.
    """

    world: World
    ledger: SideEffectLedger

    def preview_refund(
        self,
        order_id: str,
        amount_cents: Money,
        reason: str,
    ) -> dict[str, Any]:
        """Bound :func:`preview_refund`."""
        return preview_refund(
            order_id, amount_cents, reason, world=self.world
        )

    def issue_refund(
        self,
        order_id: str,
        amount_cents: Money,
        reason: str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        """Bound :func:`issue_refund`."""
        return issue_refund(
            order_id,
            amount_cents,
            reason,
            idempotency_key,
            world=self.world,
            ledger=self.ledger,
        )

    def send_message(
        self,
        order_id: str,
        body: str,
        idempotency_key: str,
        channel: str = "email",
    ) -> dict[str, Any]:
        """A write with no true inverse, so its intent is logged too."""
        self.ledger.record_intent(
            key=idempotency_key,
            tool="send_message",
            version="2",
            order_id=order_id,
            channel=channel,
        )
        record = self.world.send_message(
            order_id, body, channel, idempotency_key=idempotency_key
        )
        shaped = {
            "message_id": str(record["message_id"]),
            "status": "duplicate" if record.get("duplicate") else "sent",
        }
        self.ledger.record_outcome(key=idempotency_key, receipt=shaped)
        return shaped

    def escalate_to_specialist(
        self,
        order_id: str,
        reason: str,
        idempotency_key: str,
        notes: str = "",
    ) -> dict[str, Any]:
        """Naturally idempotent: a second escalation reuses the open case."""
        self.ledger.record_intent(
            key=idempotency_key,
            tool="escalate_to_specialist",
            version="2",
            order_id=order_id,
            reason=reason,
        )
        record = self.world.escalate_to_specialist(
            order_id, reason, notes or None, idempotency_key=idempotency_key
        )
        shaped = {
            "case_id": str(record["case_id"]),
            "status": str(record["status"]),
        }
        self.ledger.record_outcome(key=idempotency_key, receipt=shaped)
        return shaped

    def approval_threshold_cents(self) -> Money:
        """The threshold a preview compares against."""
        return REFUND_APPROVAL_THRESHOLD_CENTS
