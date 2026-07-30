"""Four ways to obtain model responses, and one environment variable.

A production agent codebase needs four, and the mistake most teams make is
having two. They trade the same three properties in a consistent direction:

===========  ============  ======  ============================
mode         determinism   cost    fidelity to a real model
===========  ============  ======  ============================
``mock``     total         zero    low, and scriptable
``record``   none          real    total, on the recording pass
``replay``   total         zero    that of the day it recorded
``live``     none          real    current
===========  ============  ======  ============================

Mock's unique power is that you can script trajectories a real model would
rarely produce, which is exactly what you need for testing recovery paths.

Replay is not a separate class. It is :class:`FakeModel` with its script
loaded from a cassette rather than typed by hand, which keeps one code path
for both. ``RecordingModel`` is the only class here that is not part of the
shared runtime contract, and it is a twenty-line pass-through.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from northstar_contracts import Message, ToolCall, ToolSpec
from northstar_runtime import (
    FakeModel,
    LiveModel,
    ModelProvider,
    ModelResponse,
)

__all__ = [
    "MODES",
    "SCRIPT_DIR",
    "RecordingModel",
    "load_cassette",
    "load_script",
    "model_for_mode",
    "mode_from_env",
]

SCRIPT_DIR = Path(__file__).resolve().parent / "scripts"

#: The four modes. ``mock`` is first because it is the default, and the
#: default is the decision that matters most: if the fallback when
#: ``MODEL_MODE`` is unset were ``live``, every forgotten variable in every
#: CI job would quietly start spending money and reintroducing variance.
MODES: tuple[str, ...] = ("mock", "replay", "record", "live")


def mode_from_env(env: dict[str, str] | None = None) -> str:
    """Read ``MODEL_MODE``, defaulting to ``mock``.

    Raises:
        ValueError: On an unknown mode. Failing closed on a typo beats
            falling back to the expensive path.
    """
    mode = (env if env is not None else dict(os.environ)).get(
        "MODEL_MODE", "mock"
    )
    if mode not in MODES:
        raise ValueError(
            f"unknown MODEL_MODE: {mode!r}; expected one of "
            f"{', '.join(MODES)}"
        )
    return mode


def load_script(name: str, directory: Path = SCRIPT_DIR) -> list[Any]:
    """Load a hand-written script from JSON.

    A step is either a string, which ends the run, or a tool call.
    """
    payload = json.loads((directory / name).read_text(encoding="utf-8"))
    steps: list[Any] = []
    for step in payload["steps"]:
        if isinstance(step, str):
            steps.append(step)
        else:
            steps.append(ToolCall.from_dict(step))
    return steps


def load_cassette(name: str, directory: Path = SCRIPT_DIR) -> list[Any]:
    """Load a recorded cassette as a script.

    The cassette holds full :class:`ModelResponse` records, so the steps
    are reconstructed from what a real model actually returned. Replay is
    therefore the same class as mock with a different source, which is why
    there is no ``ReplayModel``.
    """
    steps: list[Any] = []
    for line in (directory / name).read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        record = json.loads(line)
        if record.get("kind") != "response":
            continue
        response = ModelResponse.from_dict(record["response"])
        steps.append(
            response.tool_calls
            if response.tool_calls
            else (response.text or "")
        )
    return steps


class RecordingModel:
    """A pass-through that writes every exchange to a cassette.

    Twenty lines, and two rules that keep cassettes from rotting into
    liabilities. **Redact on write, not on read**: a recorded request
    contains the accumulated message history, which for an agent means
    customer names and whatever a tool returned three steps ago, so the
    redaction policy runs before the bytes reach the file. And **stamp the
    provenance**: model identifier, provider, and recording date, so
    :func:`cassettes.expired` can fail a suite whose evidence is testing a
    model that no longer exists.

    Args:
        base: The live provider being recorded.
        path: Cassette to append to.
        redact: Applied to every recorded payload before it is written.
        recorded_at: Provenance stamp. Supply it for a reproducible file.
    """

    def __init__(
        self,
        base: ModelProvider,
        path: str | Path,
        redact: Any = None,
        recorded_at: str = "",
    ) -> None:
        self.base = base
        self.path = Path(path)
        self.redact = redact
        self.recorded_at = recorded_at
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def complete(
        self,
        messages: list[Message],
        tools: list[ToolSpec],
    ) -> ModelResponse:
        """Call the real provider, record the exchange, return the reply."""
        response = self.base.complete(messages, tools)
        record = {
            "kind": "response",
            "provider": type(self.base).__name__,
            "model": response.model,
            "recorded_at": self.recorded_at,
            "request": [m.to_dict() for m in messages],
            "response": response.to_dict(),
        }
        if self.redact is not None:
            record = self.redact.redact(record)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, sort_keys=True) + "\n")
        return response


def model_for_mode(mode: str) -> ModelProvider:
    """MODEL_MODE picks the provider. "mock" is the default."""
    if mode == "mock":
        return FakeModel(default=load_script("refund.json"))
    if mode == "replay":          # same class, recorded script
        return FakeModel(default=load_cassette("refund.jsonl"))
    if mode == "record":
        return RecordingModel(LiveModel(), SCRIPT_DIR / "refund.jsonl")
    if mode == "live":
        return LiveModel()  # NORTHSTAR_MODEL_BASE_URL, _API_KEY
    raise ValueError(f"unknown MODEL_MODE: {mode!r}")
