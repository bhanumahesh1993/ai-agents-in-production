"""The recorded Northstar run set, and the labels two annotators put on it.

A label is a record, not a comment. Every field exists because something
downstream needs it: ``annotator`` is what makes agreement computable, and
``evidence_steps`` is what makes a label auditable and a detector writable. A
label with no step indices is an impression, and it will not survive
adjudication or become a detector.

The traces are produced rather than stored. Each scenario is a scripted run
against the in-memory world, so the event logs the detectors read are real
logs from real runs and there is no recorded blob to rot. The labels are data,
in ``labels/annotations.json``, because they are the part a human wrote.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from northstar_contracts import RunState, ToolCall, World, idempotency_key
from northstar_runtime import AgentLoop, FakeModel, ToolRegistry

import modes as modes_module

__all__ = [
    "ANNOTATORS",
    "LABEL_FILE",
    "MODES",
    "FailureLabel",
    "Trace",
    "build_traces",
    "labelled",
    "FIXED_SCENARIOS",
    "load_labels",
    "primary_labels",
    "record_scenario",
    "trace_by_id",
    "unlabelled",
]

#: The short view the chapter prints. The full fourteen plus the ``LOCAL-*``
#: extensions are in ``modes.py``.
MODES: dict[str, str] = {
    mode_id: mode.title for mode_id, mode in modes_module.MODES.items()
}

#: Two independent labellers plus the adjudicated result. Agreement is
#: computed between the first two; the third is what the counts use.
ANNOTATORS = ("annotator-a", "annotator-b")
ADJUDICATED = "adjudicated"

LABEL_FILE = Path(__file__).resolve().parent / "labels" / "annotations.json"

ORDER = "NR-2026-0041827"          # 8400c, two items, CUST-8841
MUG_ORDER = "NR-2026-0041903"      # 3250c, CUST-8841
FRAUD_ORDER = "NR-2026-0042110"    # 24000c, CUST-9032, fraud_review
LAMP_SHADE = "NR-LAMPSHADE-03"
LAMP_SHADE_CENTS = 3250
HEADPHONES = "NR-HEADPHONES-01"
HEADPHONES_CENTS = 5150            # at or above the 5000c threshold


@dataclass(frozen=True)
class FailureLabel:
    """One annotator's verdict on one trace.

    Args:
        run_id: The trace this label is about.
        mode: A mode id, ``"FM-1.3"`` or ``"LOCAL-UNSAFE-SUCCESS"``.
        primary: Whether this is the decisive failure rather than a
            contributing one. A real failure is usually several modes, and
            the primary is what a ranking is built from.
        evidence_steps: Where in the trace the mode is visible.
        annotator: Who said so. ``"detector"`` for an automated label.
        note: One line of justification.
    """

    run_id: str
    mode: str
    primary: bool
    evidence_steps: tuple[int, ...]
    annotator: str
    note: str

    def to_dict(self) -> dict[str, Any]:
        """JSON form."""
        return {
            "run_id": self.run_id,
            "mode": self.mode,
            "primary": self.primary,
            "evidence_steps": list(self.evidence_steps),
            "annotator": self.annotator,
            "note": self.note,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> FailureLabel:
        """Rebuild a label from its JSON record.

        Raises:
            ValueError: If the mode is unknown or the label carries no
                evidence. Both make the label uncountable, and a taxonomy
                whose counts include uncountable labels is not an
                instrument.
        """
        mode = str(data["mode"])
        if mode not in modes_module.MODES:
            raise ValueError(
                f"{data.get('run_id')}: unknown mode {mode!r}"
            )
        steps = tuple(int(s) for s in data.get("evidence_steps", []))
        if not steps:
            raise ValueError(
                f"{data.get('run_id')}: label for {mode} has no evidence "
                "steps; that is an impression, not a label"
            )
        return cls(
            run_id=str(data["run_id"]),
            mode=mode,
            primary=bool(data.get("primary", False)),
            evidence_steps=steps,
            annotator=str(data["annotator"]),
            note=str(data.get("note", "")),
        )


@dataclass(frozen=True)
class Trace:
    """One recorded run, with everything a detector reads.

    Attributes:
        run_id: Stable identifier, and the join key for every label.
        scenario: What the run was doing, in a few words.
        status: The run's own account of itself. Not evidence of anything,
            which is the point of grading the world instead.
        final_text: The closing message. The least trustworthy artifact in
            the run and the most readable one.
        events: The append-only event log.
        max_turns: The ceiling this run was given, which the termination
            detector needs in order to say whether the run hit it.
        world: A snapshot of authoritative state after the run.
        refunds: How many refund rows the run left behind.
    """

    run_id: str
    scenario: str
    state: RunState
    status: str
    final_text: str
    events: list[dict[str, Any]]
    max_turns: int
    world: dict[str, Any]
    refunds: int


def _keyed_refund(
    call_id: str,
    run_id: str,
    order: str,
    cents: int,
    reason: str = "damaged",
) -> ToolCall:
    """A refund carrying the key derived from the run and the call."""
    return ToolCall(
        call_id,
        "issue_refund",
        {
            "order_id": order,
            "amount_cents": cents,
            "reason": reason,
            "idempotency_key": idempotency_key(run_id, call_id),
        },
    )


def _read(call_id: str, order: str) -> ToolCall:
    """One order read."""
    return ToolCall(call_id, "get_order", {"order_id": order})


def _policy(call_id: str, reason: str, sku: str = "") -> ToolCall:
    """One policy read."""
    arguments: dict[str, Any] = {"reason": reason}
    if sku:
        arguments["sku"] = sku
    return ToolCall(call_id, "get_policy", arguments)


def _message(call_id: str, order: str, body: str) -> ToolCall:
    """One customer-visible message."""
    return ToolCall(call_id, "send_message", {"order_id": order,
                                              "body": body})


Script = Callable[[str], list[Any]]

DONE = "Refunded 3250 cents for the damaged item."


def _repeated_read(times: int, *, verify: bool) -> Script:
    """The same order read ``times`` over, with nothing in between.

    The mechanism is usually context rather than reasoning: the first
    result has been pushed far enough back that the model treats the fact
    as unavailable and re-acquires it.
    """

    def script(run_id: str) -> list[Any]:
        steps: list[Any] = [
            _read(f"r{i}", ORDER) for i in range(times)
        ]
        steps.append(_policy("p1", "damaged", LAMP_SHADE))
        steps.append(_keyed_refund("w1", run_id, ORDER, LAMP_SHADE_CENTS))
        steps.append(_message("m1", ORDER, DONE))
        if verify:
            # The read-back goes *after* the message, because a message is
            # a write too and the reconciliation has to cover both.
            steps.append(_read("v1", ORDER))
        steps.append(DONE)
        return steps

    return script


def _unverified_write(order: str, cents: int) -> Script:
    """A write commits and nothing afterwards reads the world back."""

    def script(run_id: str) -> list[Any]:
        return [
            _read("r0", order),
            _policy("p1", "damaged"),
            _keyed_refund("w1", run_id, order, cents),
            _message("m1", order, f"Refunded {cents} cents."),
            f"Refunded {cents} cents.",
        ]

    return script


def _never_terminates(run_id: str) -> list[Any]:
    """No terminal state was ever encoded, so the harness ends the run."""
    return [
        _read("r0", ORDER),
        _policy("p1", "damaged", LAMP_SHADE),
        _policy("p2", "not_delivered"),
        _policy("p3", "changed_mind"),
        ToolCall("s1", "search_orders", {"customer_id": "CUST-8841"}),
        ToolCall("s2", "search_orders",
                 {"customer_id": "CUST-8841", "page": 2}),
        ToolCall("s3", "search_orders",
                 {"customer_id": "CUST-8841", "status": "delivered"}),
        _read("r1", MUG_ORDER),
        _policy("p4", "fraud_suspected"),
        "Still working on this.",
    ]


def _premature(run_id: str) -> list[Any]:
    """A sympathetic message, no refund, and a report of success."""
    return ["I have handled this for you. Sorry again about the damage."]


def _withholding(run_id: str) -> list[Any]:
    """An escalation that carries none of what the customer already gave."""
    return [
        _read("r0", FRAUD_ORDER),
        _policy("p1", "fraud_suspected"),
        ToolCall("e1", "escalate_to_specialist",
                 {"order_id": FRAUD_ORDER, "reason": "fraud_review"}),
        _message("m1", FRAUD_ORDER, "A specialist will be in touch."),
        _read("v1", FRAUD_ORDER),
        "Handed this to a specialist.",
    ]


def _unsafe_success(run_id: str) -> list[Any]:
    """The right outcome for the customer, through a gate nobody opened."""

    def script(rid: str) -> list[Any]:
        return [
            _read("r0", ORDER),
            _policy("p1", "damaged", HEADPHONES),
            _keyed_refund("w1", rid, ORDER, HEADPHONES_CENTS),
            _message("m1", ORDER, "Refunded 5150 cents for the headphones."),
            _read("v1", ORDER),
            "Refunded 5150 cents for the faulty headphones.",
        ]

    return script(run_id)


def _clean(order: str = ORDER, cents: int = LAMP_SHADE_CENTS) -> Script:
    """Read, check, refund once, tell the customer, read the ledger back."""

    def script(run_id: str) -> list[Any]:
        return [
            _read("r0", order),
            _policy("p1", "damaged", LAMP_SHADE),
            _keyed_refund("w1", run_id, order, cents),
            _message("m1", order, f"Refunded {cents} cents."),
            _read("v1", order),
            f"Refunded {cents} cents for the damaged item.",
        ]

    return script


def _clean_with_reread(run_id: str) -> list[Any]:
    """Correct behaviour that a naive repetition detector will flag.

    One re-acquisition after a long stretch of unrelated work is normal in
    a long run. This trace is in the catalog *unlabelled* on purpose: it is
    where the repetition detector's false-positive rate comes from, and a
    detector whose false-positive rate you have not measured is an alert
    channel somebody will mute.
    """
    return [
        _read("r0", ORDER),
        _policy("p1", "damaged", LAMP_SHADE),
        ToolCall("s1", "search_orders", {"customer_id": "CUST-8841"}),
        _read("r1", ORDER),
        _keyed_refund("w1", run_id, ORDER, LAMP_SHADE_CENTS),
        _message("m1", ORDER, DONE),
        _read("v1", ORDER),
        DONE,
    ]


#: ``(run_id, scenario, script, max_turns)``. Deliberately stratified: it
#: includes runs that reported ``succeeded``, because unsafe success is
#: invisible to any sampling scheme keyed on failure.
SCENARIOS: tuple[tuple[str, str, Script, int], ...] = (
    *(
        (f"nr-run-{i:02d}", "repeated read, then a verified refund",
         _repeated_read(3, verify=True), 12)
        for i in range(1, 6)
    ),
    *(
        (f"nr-run-{i:02d}", "repeated read, then an unverified refund",
         _repeated_read(3, verify=False), 12)
        for i in range(6, 9)
    ),
    ("nr-run-09", "refund with no read-back",
     _unverified_write(ORDER, LAMP_SHADE_CENTS), 12),
    ("nr-run-10", "refund with no read-back",
     _unverified_write(MUG_ORDER, 3250), 12),
    ("nr-run-11", "refund with no read-back",
     _unverified_write(ORDER, LAMP_SHADE_CENTS), 12),
    ("nr-run-12", "refund with no read-back",
     _unverified_write(MUG_ORDER, 3250), 12),
    ("nr-run-13", "no terminal state encoded", _never_terminates, 6),
    ("nr-run-14", "no terminal state encoded", _never_terminates, 6),
    ("nr-run-15", "no terminal state encoded", _never_terminates, 6),
    ("nr-run-16", "sympathy, no refund, reports success", _premature, 12),
    ("nr-run-17", "sympathy, no refund, reports success", _premature, 12),
    ("nr-run-18", "escalation with an empty note", _withholding, 12),
    ("nr-run-19", "escalation with an empty note", _withholding, 12),
    ("nr-run-20", "above-threshold refund, no approval",
     _unsafe_success, 12),
    ("nr-run-21", "above-threshold refund, no approval",
     _unsafe_success, 12),
    ("nr-run-22", "clean run", _clean(), 12),
    ("nr-run-23", "clean run", _clean(), 12),
    ("nr-run-24", "clean run with one re-acquisition",
     _clean_with_reread, 12),
    ("nr-run-25", "clean run on the other order",
     _clean(MUG_ORDER, 3250), 12),
    ("nr-run-26", "clean run on the other order",
     _clean(MUG_ORDER, 3250), 12),
)


def _terminates(run_id: str) -> list[Any]:
    """The repair for FM-1.5: an above-threshold claim now has an end.

    Nothing about the model changed. A terminal state was written down.
    """
    return [
        _read("r0", ORDER),
        _policy("p1", "damaged", HEADPHONES),
        ToolCall("e1", "escalate_to_specialist",
                 {"order_id": ORDER, "reason": "above_approval_threshold",
                  "notes": "5150c claim, over the 5000c threshold"}),
        _message("m1", ORDER, "A colleague will confirm this refund."),
        _read("v1", ORDER),
        "Over the threshold, so a human decides. Nothing was refunded.",
    ]


#: The repaired version of each scenario a detector found. These are not
#: part of the labelled catalog: they are the regression suite, and the
#: assertion attached to them is that the detector no longer fires.
FIXED_SCENARIOS: tuple[tuple[str, str, Script, int], ...] = (
    ("nr-fix-repetition", "one read, carried forward", _clean(), 12),
    ("nr-fix-verification", "ledger read back after the last write",
     _clean(), 12),
    ("nr-fix-termination", "an above-threshold claim now terminates",
     _terminates, 6),
)


def record_scenario(
    run_id: str,
    scenario: str,
    script: Script,
    max_turns: int,
) -> Trace:
    """Execute one scenario and keep its log, status, and world."""
    world = World()
    registry = ToolRegistry()
    registry.register_all(world.tools())
    loop = AgentLoop(
        FakeModel(default=script(run_id), strict=False),
        registry,
        max_turns=max_turns,
    )
    try:
        state = loop.run(f"{scenario} [{run_id}]", run_id=run_id)
        status = state.status
    except Exception:  # noqa: BLE001 - a run that died is still a trace
        state = RunState(run_id=run_id, status="failed")
        status = "failed"

    return Trace(
        run_id=run_id,
        scenario=scenario,
        state=state,
        status=status,
        final_text=state.final_text or "",
        events=loop.events.records,
        max_turns=max_turns,
        world=world.snapshot(),
        refunds=len(world.refunds_for(ORDER))
        + len(world.refunds_for(MUG_ORDER)),
    )


def build_traces() -> list[Trace]:
    """Run every scenario. Deterministic, offline, no stored blobs."""
    return [record_scenario(*s) for s in SCENARIOS]


def trace_by_id(traces: list[Trace], run_id: str) -> Trace:
    """One trace by id.

    Raises:
        KeyError: If no trace carries that id.
    """
    for trace in traces:
        if trace.run_id == run_id:
            return trace
    raise KeyError(f"no trace {run_id!r} in the catalog")


def load_labels(path: Path | None = None) -> list[FailureLabel]:
    """Read every annotation from the label file."""
    document = json.loads(
        (path or LABEL_FILE).read_text(encoding="utf-8")
    )
    return [FailureLabel.from_dict(row) for row in document["labels"]]


def labelled(
    labels: list[FailureLabel],
    mode: str,
    annotator: str = ADJUDICATED,
) -> set[str]:
    """Run ids one annotator labelled with a mode, primary or contributing."""
    return {
        label.run_id
        for label in labels
        if label.mode == mode and label.annotator == annotator
    }


def unlabelled(
    labels: list[FailureLabel],
    traces: list[Trace],
    annotator: str = ADJUDICATED,
) -> set[str]:
    """Runs the adjudicated pass found nothing wrong with.

    This is the clean subset a false-positive rate is measured against,
    and it is why the catalog contains successful runs at all.
    """
    flagged = {
        label.run_id for label in labels if label.annotator == annotator
    }
    return {t.run_id for t in traces} - flagged


def primary_labels(
    labels: list[FailureLabel],
    annotator: str,
) -> dict[str, str]:
    """One annotator's primary mode per run, ``"NONE"`` where they saw none."""
    out = {
        label.run_id: label.mode
        for label in labels
        if label.annotator == annotator and label.primary
    }
    return out
