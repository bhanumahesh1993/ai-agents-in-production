"""Three structural detectors, and the tiering they sit in.

Detectors come in three tiers and the tier you can reach determines how much
the detector is worth.

**Structural** detectors read the event log and answer a question about shape.
They are deterministic, cheap enough to run on every production trace, and
they carry no model risk. All three here are structural, which is a happy
accident of how the top modes are defined rather than a general rule.

**State** detectors compare the run's claim against the world. FM-3.1 and
FM-3.3 need one, because a run that stopped early is structurally identical to
a run that finished.

**Judge** detectors ask a model whether a trace exhibits a mode. Reserve them
for the modes that resist both other tiers, mostly the inter-agent
misalignment family, and never let one file a ticket before its agreement with
your human labels has been measured.
"""

from __future__ import annotations

from collections.abc import Sequence

from catalog import FailureLabel, Trace

from detectors.repetition import detect_step_repetition
from detectors.termination import detect_termination_unawareness
from detectors.verification import detect_unverified_write

__all__ = [
    "DETECTORS",
    "detect_step_repetition",
    "detect_termination_unawareness",
    "detect_unverified_write",
    "fired",
    "run_all",
    "run_detector",
]

#: Detector name to the mode it claims. One detector, one mode: a detector
#: that claims two modes at once is a detector nobody can calibrate.
DETECTORS: dict[str, str] = {
    "detect_step_repetition": "FM-1.3",
    "detect_termination_unawareness": "FM-1.5",
    "detect_unverified_write": "FM-3.2",
}


def run_detector(name: str, trace: Trace) -> FailureLabel | None:
    """Run one detector over one trace.

    Raises:
        KeyError: On an unknown detector name.
    """
    if name == "detect_step_repetition":
        return detect_step_repetition(trace.run_id, trace.events)
    if name == "detect_termination_unawareness":
        return detect_termination_unawareness(
            trace.run_id, trace.events, trace.max_turns
        )
    if name == "detect_unverified_write":
        return detect_unverified_write(trace.state, trace.events)
    known = ", ".join(sorted(DETECTORS))
    raise KeyError(f"no detector {name!r}; known detectors: {known}")


def run_all(traces: Sequence[Trace]) -> dict[str, list[FailureLabel]]:
    """Every detector over every trace, in one pass."""
    out: dict[str, list[FailureLabel]] = {name: [] for name in DETECTORS}
    for trace in traces:
        for name in DETECTORS:
            label = run_detector(name, trace)
            if label is not None:
                out[name].append(label)
    return out


def fired(labels: Sequence[FailureLabel]) -> set[str]:
    """The set of run ids one detector fired on."""
    return {label.run_id for label in labels}
