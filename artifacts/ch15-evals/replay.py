"""The replay tier: recorded model responses, re-executed tools, pinned config.

Replay re-runs a recorded trajectory against recorded model decisions. Because
every nondeterministic input is pinned, a replay run is fully deterministic and
costs nothing but CPU, which makes it a fast, precise regression detector for
the things that break most often: a prompt template change that alters tool
selection, a tool schema change that breaks argument parsing, a policy change
that starts denying a call it used to allow, a context change that drops the
field a later step needed.

What replay cannot catch is the entire class of failures caused by the model
deciding something different, because the recording already decided for it. A
replay suite that passes tells you your code did not regress against last
month's model behaviour. It tells you nothing about this month's. That is why
the gate has two tiers and why this one is only the first.

Tool results are re-executed rather than played back, and the recorded ones
are compared against the live ones. Playing results back would make the tier
pass even when the tool layer had broken underneath it, which is the one thing
the tier is supposed to notice.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from northstar_contracts import (
    EventLog,
    Message,
    RunState,
    ToolSpec,
    World,
)
from northstar_runtime import AgentLoop, ModelResponse

import cases
from cases import Case, CaseRun, EventSink, build_registry
from sim.world import from_fixture

__all__ = [
    "FIXTURE_DIR",
    "Fixture",
    "RecordedModel",
    "ReplayDivergence",
    "ReplayResult",
    "load_fixture",
    "load_fixtures",
    "record",
    "replay",
    "save_fixture",
]

#: Where the recorded trajectories live. Dated, because a golden trajectory
#: older than the model version it was recorded against is a source of false
#: failures rather than a source of truth.
FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures" / "2026-07"


class ReplayDivergence(RuntimeError):
    """The replay took a different path from the recording.

    Raised loudly rather than papered over. A silent divergence is worse
    than a crash, because it turns a regression detector into a green
    light.
    """


@dataclass(frozen=True)
class Fixture:
    """One recorded run.

    Attributes:
        case_id: Which case this recorded.
        run_id: The run id used, which is what the derived idempotency
            keys in the recording were computed from. Replaying under a
            different run id would change every key.
        config_hash: The case configuration the recording was made under.
            A replay against a different hash is a replay of something
            else, and the gate refuses it.
        responses: The model's turns, in order.
        observations: ``(tool, ok)`` for every tool result, in order.
    """

    case_id: str
    run_id: str
    config_hash: str
    responses: list[dict[str, Any]] = field(default_factory=list)
    observations: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """JSON form."""
        return {
            "case_id": self.case_id,
            "run_id": self.run_id,
            "config_hash": self.config_hash,
            "responses": list(self.responses),
            "observations": list(self.observations),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Fixture:
        """Rebuild from JSON.

        Raises:
            ValueError: If the config hash is missing. A recording that
                does not say what configuration produced it cannot be
                replayed against that configuration, and Chapter 15's rule
                is that an ungradeable run is a failure, not a skip.
        """
        if not data.get("config_hash"):
            raise ValueError(
                f"fixture for {data.get('case_id')!r} has no config_hash; "
                "it cannot be replayed against the configuration it had"
            )
        return cls(
            case_id=str(data["case_id"]),
            run_id=str(data["run_id"]),
            config_hash=str(data["config_hash"]),
            responses=list(data.get("responses", [])),
            observations=list(data.get("observations", [])),
        )


class RecordedModel:
    """Replays recorded model turns and refuses to invent new ones."""

    def __init__(self, responses: list[dict[str, Any]]) -> None:
        self.responses = list(responses)
        self.turn = 0

    def complete(
        self,
        messages: list[Message],
        tools: list[ToolSpec],
    ) -> ModelResponse:
        """Return the next recorded turn.

        Raises:
            ReplayDivergence: If the loop asks for a turn the recording
                does not have. That means the code under test took a path
                the recording never did, which is the finding.
        """
        if self.turn >= len(self.responses):
            raise ReplayDivergence(
                f"the recording has {len(self.responses)} turn(s) and the "
                f"loop asked for turn {self.turn + 1}"
            )
        response = ModelResponse.from_dict(self.responses[self.turn])
        self.turn += 1
        return response


@dataclass(frozen=True)
class ReplayResult:
    """What one replay found."""

    case_id: str
    passed: bool
    divergences: tuple[str, ...]
    state: RunState
    world: World

    def summary(self) -> str:
        """One line for the gate's log."""
        verdict = "ok" if self.passed else "DIVERGED"
        detail = f" ({self.divergences[0]})" if self.divergences else ""
        return f"{self.case_id}: {verdict}{detail}"


def record(case: Case) -> Fixture:
    """Run a case once and keep what a replay would need.

    The model's decisions are recorded because they are the
    nondeterministic input. The tool results are recorded so a replay can
    notice that the tool layer answered differently, not so it can avoid
    calling the tools.
    """
    run = cases.run_case(case)
    return Fixture(
        case_id=case.case_id,
        run_id=run.run_id,
        config_hash=case.config_hash,
        responses=_responses_of(run),
        observations=[
            {"tool": e["payload"].get("tool"), "ok": e["payload"].get("ok")}
            for e in run.events
            if e["type"] == "tool.result"
        ],
    )


def _responses_of(run: CaseRun) -> list[dict[str, Any]]:
    """Rebuild the model's turns from the run's assistant messages."""
    responses: list[dict[str, Any]] = []
    for message in run.state.messages:
        if message.role != "assistant":
            continue
        calls = message.tool_calls
        text = message.content if isinstance(message.content, str) else None
        if text is None and isinstance(message.content, list):
            for block in message.content:
                if isinstance(block, dict) and block.get("type") == "text":
                    text = str(block.get("text", ""))
        responses.append(
            ModelResponse(
                text=text,
                tool_calls=calls,
                model="replay",
                stop_reason="tool_use" if calls else "end_turn",
            ).to_dict()
        )
    return responses


def save_fixture(fixture: Fixture, directory: Path | None = None) -> Path:
    """Write one fixture to disk and return where it landed."""
    target = (directory or FIXTURE_DIR)
    target.mkdir(parents=True, exist_ok=True)
    path = target / f"{fixture.case_id}.json"
    path.write_text(
        json.dumps(fixture.to_dict(), indent=2) + "\n", encoding="utf-8"
    )
    return path


def load_fixture(case_id: str, directory: Path | None = None) -> Fixture:
    """Load one recorded run.

    Raises:
        FileNotFoundError: If no recording exists for the case.
    """
    path = (directory or FIXTURE_DIR) / f"{case_id}.json"
    if not path.exists():
        raise FileNotFoundError(
            f"no replay fixture for {case_id!r} at {path}. Record one with "
            "`python artifacts/ch15-evals/replay.py --record`."
        )
    return Fixture.from_dict(json.loads(path.read_text(encoding="utf-8")))


def load_fixtures(directory: Path | None = None) -> list[Fixture]:
    """Every recording in a fixture directory, by case id."""
    target = directory or FIXTURE_DIR
    return [
        Fixture.from_dict(json.loads(p.read_text(encoding="utf-8")))
        for p in sorted(target.glob("*.json"))
    ]


def replay(fixture: Fixture, case: Case) -> ReplayResult:
    """Re-run a recording against the current code.

    Args:
        fixture: The recording.
        case: The case as it is configured *now*.

    Returns:
        A :class:`ReplayResult`. A configuration change, a different tool
        answer, or a path the recording does not cover all come back as
        divergences rather than as exceptions, so one bad case does not
        take the tier down with it.
    """
    divergences: list[str] = []
    if fixture.config_hash != case.config_hash:
        divergences.append(
            "config hash moved since the recording: "
            f"{fixture.config_hash[:12]} -> {case.config_hash[:12]}"
        )

    world = from_fixture(case.fixture)
    for tool, kind, times in case.faults:
        world.inject_fault(tool, kind=kind, times=times)
    user = case.user()
    model = RecordedModel(fixture.responses)
    loop = AgentLoop(
        model,                                  # type: ignore[arg-type]
        build_registry(world, user),
        max_turns=case.max_turns + 4,
        budget_cents=case.budget_cents,
    )
    log = EventLog()
    loop.telemetry = EventSink(log)

    try:
        state = loop.run(user.goal, run_id=fixture.run_id)
    except ReplayDivergence as exc:
        divergences.append(str(exc))
        state = RunState(run_id=fixture.run_id, status="failed")
    except Exception as exc:  # noqa: BLE001
        divergences.append(f"{type(exc).__name__}: {exc}")
        state = RunState(run_id=fixture.run_id, status="failed")

    observed = [
        {"tool": e["payload"].get("tool"), "ok": e["payload"].get("ok")}
        for e in log.records
        if e["type"] == "tool.result"
    ]
    if observed != fixture.observations:
        divergences.append(
            f"tool results differ: recorded {len(fixture.observations)} "
            f"observation(s), replay produced {len(observed)}"
        )

    return ReplayResult(
        case_id=case.case_id,
        passed=not divergences,
        divergences=tuple(divergences),
        state=state,
        world=world,
    )


def record_all(directory: Path | None = None) -> list[Path]:
    """Record every case in the suite. Used to refresh the fixtures."""
    return [save_fixture(record(c), directory) for c in cases.CASES]


if __name__ == "__main__":  # pragma: no cover - a maintenance entry point
    import sys

    if "--record" in sys.argv[1:]:
        written = record_all()
        print(f"recorded {len(written)} fixture(s) into {FIXTURE_DIR}")
    else:
        print("usage: python artifacts/ch15-evals/replay.py --record")
