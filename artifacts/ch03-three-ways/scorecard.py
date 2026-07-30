"""Score three runtimes on the seven criteria, by measurement where possible.

The mechanical columns are filled by running the thing, never by reading a
feature matrix:

* **glue** — logical source lines in the port's ``build``/``run``/``resume``,
  docstrings and comments excluded. The lines you must write.
* **ckpt** — checkpoint writes the runtime performed during one run.
* **resume** — the step a fresh process resumed from after the worker was
  killed with the refund committed and unrecorded. ``0`` means it replayed
  the whole run.
* **policy** — whether a synchronous decision point of *yours* was actually
  consulted before dispatch. Measured by counting evaluations, so a runtime
  that accepts the argument and ignores it scores ``no``.
* **spans** — events that reached a collector you control.
* **egress** — bytes of tool arguments that left the process by default.

Criteria one and six — control, and the cost of exit — are judgment calls.
They are printed as questions rather than scored, because a number invented
for them would be the feature grid this chapter argues against.
"""

from __future__ import annotations

import inspect
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

import shared.triage as triage
from northstar_contracts import RunState, ToolCall
from northstar_evals import trajectory
from northstar_policy import Decision, PolicyEngine, Principal, default_northstar_policy
from northstar_runtime import SimulatedCrash
from ports.graph import GraphPort
from ports.harness import HarnessPort, reset_sessions
from ports.raw import RawLoopPort

__all__ = [
    "PORTS",
    "PORT_CLASSES",
    "CountingPolicy",
    "LocalSink",
    "PortScore",
    "close_port",
    "glue_lines",
    "load_port",
    "print_scorecard",
    "run_once",
    "score_all",
    "score_port",
]

#: The three implementations, in the order the chapter introduces them.
PORTS = ["raw", "graph", "harness"]

PORT_CLASSES: dict[str, type] = {
    "raw": RawLoopPort,
    "graph": GraphPort,
    "harness": HarnessPort,
}

#: A separate run id for the kill-and-resume measurement, so it cannot be
#: confused with the clean run's checkpoint.
CRASH_RUN_ID = "run_01H3KILL"

#: The judgment calls. Printed as prompts; deliberately not scored.
OPEN_QUESTIONS = (
    "control: which of these seams do you need to see next quarter?",
    "cloud alignment: which runtime is native where you already deploy?",
    "exit: what would you rewrite to leave — tools, state, or the loop?",
)


class LocalSink:
    """A collector you run. Counts what reached it."""

    def __init__(self) -> None:
        self.records: list[dict[str, Any]] = []

    def emit(self, record: dict[str, Any]) -> None:
        """Accept one event."""
        self.records.append(record)


class CountingPolicy:
    """A decision point that records having been asked.

    Wrapping the real engine rather than replacing it keeps the measurement
    honest: the run still has to pass the same rules, so a port cannot score
    well here by being allowed to skip them.
    """

    def __init__(self, inner: PolicyEngine | None = None) -> None:
        self.inner = inner or default_northstar_policy()
        self.evaluated: list[str] = []

    def evaluate(
        self,
        principal: Principal,
        call: ToolCall,
        ctx: dict[str, Any],
    ) -> Decision:
        """Record the question, then let the real engine answer it."""
        self.evaluated.append(call.name)
        return self.inner.evaluate(principal, call, ctx)

    @property
    def saw_the_write(self) -> bool:
        """Whether the write tool was put to this decision point at all."""
        return "issue_refund" in self.evaluated


@dataclass(frozen=True)
class PortScore:
    """One row of the scorecard. Every field is measured, none is asserted."""

    port: str
    glue_lines: int
    checkpoint_writes: int
    resumed_from_step: int
    policy_consulted: bool
    spans_local: int
    egress_bytes: int
    trajectory: tuple[str, ...]
    refunds: tuple[int, ...]
    refunds_after_replay: tuple[int, ...]
    refunds_after_kill: tuple[int, ...]


def glue_lines(port_class: type) -> int:
    """Count the logical lines the port's three methods cost you.

    Docstrings, comments, and blank lines are excluded, because a comment is
    not glue. The ``def`` line is counted: you had to write it.
    """
    total = 0
    for name in ("build", "run", "resume"):
        method = getattr(port_class, name, None)
        if method is None:
            continue
        source = inspect.getsource(method)
        total += _logical_lines(source)
    return total


def _logical_lines(source: str) -> int:
    """Lines of code in a source string, minus docstrings and comments."""
    count = 0
    in_doc = False
    for raw in source.splitlines():
        line = raw.strip()
        if not line:
            continue
        if in_doc:
            if line.endswith('"""') or line.endswith("'''"):
                in_doc = False
            continue
        if line.startswith('"""') or line.startswith("'''"):
            body = line[3:]
            if not (body.endswith('"""') or body.endswith("'''")):
                in_doc = True
            continue
        if line.startswith("#"):
            continue
        count += 1
    return count


def load_port(
    port_name: str,
    policy: PolicyEngine | None = None,
    telemetry: object | None = None,
) -> Any:
    """Build one port by the name the chapter uses for it."""
    try:
        cls = PORT_CLASSES[port_name]
    except KeyError:
        known = ", ".join(PORTS)
        raise ValueError(
            f"unknown port {port_name!r}; expected one of {known}"
        ) from None
    return cls(policy=policy, telemetry=telemetry)


def run_once(
    port_name: str,
    world: Any,
    run_id: str = triage.RUN_ID,
    policy: PolicyEngine | None = None,
    telemetry: object | None = None,
) -> tuple[Any, RunState]:
    """Build a port over ``world`` and run the triage task once."""
    port = load_port(port_name, policy=policy, telemetry=telemetry)
    port.build(triage.model_for(run_id), triage.registry(world), triage.SPECS)
    return port, port.run(triage.GOAL, run_id)


def close_port(port: Any) -> None:
    """Release whatever the runtime is holding open."""
    closer = getattr(port, "close", None)
    if closer is not None:
        closer()


def _kill_and_resume(port_name: str) -> tuple[int, tuple[int, ...], RunState]:
    """Kill a worker with the refund committed, then resume on a new one.

    Returns the step the fresh process resumed from, the resulting ledger,
    and the final state. The ledger is the only one of the three that
    decides whether the runtime got it right.
    """
    world = triage.fresh_world()
    dying = load_port(port_name)
    try:
        dying.build(
            triage.model_for(CRASH_RUN_ID),
            triage.registry(world, crash_after="issue_refund"),
            triage.SPECS,
        )
        try:
            dying.run(triage.GOAL, CRASH_RUN_ID)
        except SimulatedCrash:
            pass
        else:  # pragma: no cover - the injector is under test elsewhere
            raise AssertionError("the worker was supposed to die")
    finally:
        close_port(dying)

    survivor = load_port(port_name)
    try:
        survivor.build(
            triage.model_for(CRASH_RUN_ID),
            triage.registry(world),
            triage.SPECS,
        )
        state = survivor.resume(CRASH_RUN_ID)
        resumed_from = int(survivor.resumed_from_step or 0)
    finally:
        close_port(survivor)
    return resumed_from, tuple(triage.refund_amounts(world)), state


def score_port(port_name: str) -> PortScore:
    """Measure one runtime on every column that can be measured."""
    triage.forget_checkpoints()
    reset_sessions()

    world = triage.fresh_world()
    policy = CountingPolicy()
    sink = LocalSink()
    port, state = run_once(port_name, world, policy=policy, telemetry=sink)
    try:
        refunds = tuple(triage.refund_amounts(world))
        # Read the per-run counters before the replay doubles them.
        measured = (
            int(port.checkpoint_writes),
            len(sink.records),
            int(port.vendor_bytes),
        )

        # Replay: the same run id, the same derived key, the same ledger.
        port.run(triage.GOAL, triage.RUN_ID)
        after_replay = tuple(triage.refund_amounts(world))
    finally:
        close_port(port)

    resumed_from, after_kill, _ = _kill_and_resume(port_name)

    return PortScore(
        port=port_name,
        glue_lines=glue_lines(PORT_CLASSES[port_name]),
        checkpoint_writes=measured[0],
        resumed_from_step=resumed_from,
        policy_consulted=policy.saw_the_write,
        spans_local=measured[1],
        egress_bytes=measured[2],
        trajectory=tuple(trajectory(state)),
        refunds=refunds,
        refunds_after_replay=after_replay,
        refunds_after_kill=after_kill,
    )


def score_all(ports: list[str] | None = None) -> list[PortScore]:
    """Measure every port and hand back the rows."""
    scores = [score_port(name) for name in (ports or PORTS)]
    triage.forget_checkpoints()
    return scores


def print_scorecard(scores: list[PortScore]) -> None:
    """Print the mechanical columns, then the questions nobody can score."""
    header = (
        f"{'port':<9}{'glue':>6}{'ckpt':>6}{'resume':>8}"
        f"{'policy':>8}{'spans':>7}{'egress':>8}"
    )
    print(header)
    print("-" * len(header))
    for s in scores:
        print(
            f"{s.port:<9}{s.glue_lines:>6}{s.checkpoint_writes:>6}"
            f"{s.resumed_from_step:>8}"
            f"{('yes' if s.policy_consulted else 'no'):>8}"
            f"{s.spans_local:>7}{s.egress_bytes:>8}"
        )
    print()
    print("glue   = logical lines in build/run/resume")
    print("ckpt   = checkpoint writes during one run")
    print("resume = step a new process picked up from after the kill")
    print("policy = your decision point was consulted before dispatch")
    print("spans  = events that reached a collector you control")
    print("egress = bytes of tool arguments that left by default")
    print()
    print("not scored, because a number here would be a feature grid:")
    for question in OPEN_QUESTIONS:
        print(f"  - {question}")
