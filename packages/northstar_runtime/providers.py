"""Model providers: fake, flaky, and live.

Mock mode is the default mode in this repository, and :class:`FakeModel` is
why. Every example, test, and chapter artifact runs against a scripted
model that costs nothing, needs no key, and returns the same thing every
time. That is not a limitation of the examples; it is the development loop
this book argues for. You cannot debug a harness and a model at the same
time, so pin one.

The three providers form a ladder:

* :class:`FakeModel` — deterministic. Use it for everything until the
  harness is right.
* :class:`FlakyModel` — deterministic *given a seed*, but it repeats
  itself, stalls, and gives up early, the way real models do under load.
  Use it to prove your recovery paths work.
* :class:`LiveModel` — a real provider. Opt-in, never imported by default,
  and the only one that can cost money or fail because of someone else's
  outage.
"""

from __future__ import annotations

import os
import random
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol, runtime_checkable

from northstar_contracts import (
    Message,
    ToolCall,
    ToolSpec,
    estimate_tokens,
)

__all__ = [
    "FakeModel",
    "FlakyModel",
    "LiveModel",
    "LiveModelUnavailable",
    "ModelProvider",
    "ModelResponse",
    "ScriptExhausted",
    "ScriptStep",
    "StopReason",
]

StopReason = Literal["tool_use", "end_turn", "max_tokens", "error"]


@dataclass(frozen=True)
class ModelResponse:
    """One model turn, normalised across providers.

    Normalising here rather than at each call site is what lets the same
    agent loop run against a scripted fake, a real provider, and a journal
    replay without noticing the difference.
    """

    text: str | None = None
    tool_calls: list[ToolCall] = field(default_factory=list)
    input_tokens: int = 0
    output_tokens: int = 0
    model: str = "fake-model-1"
    stop_reason: StopReason = "end_turn"

    @property
    def total_tokens(self) -> int:
        """Input plus output tokens for this turn."""
        return self.input_tokens + self.output_tokens

    def as_message(self) -> Message:
        """Render the response as the assistant message to append.

        Tool requests become ``tool_use`` content blocks, which is how
        every major provider models them and how
        :attr:`northstar_contracts.models.Message.tool_calls` reads them
        back out of a checkpoint.
        """
        if not self.tool_calls:
            return Message(role="assistant", content=self.text or "")
        blocks: list[dict[str, Any]] = []
        if self.text:
            blocks.append({"type": "text", "text": self.text})
        for call in self.tool_calls:
            blocks.append(
                {
                    "type": "tool_use",
                    "id": call.id,
                    "name": call.name,
                    "input": call.arguments,
                }
            )
        return Message(role="assistant", content=blocks)

    def to_dict(self) -> dict[str, Any]:
        """JSON-serialisable form. The durable journal stores this."""
        return {
            "text": self.text,
            "tool_calls": [c.to_dict() for c in self.tool_calls],
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "model": self.model,
            "stop_reason": self.stop_reason,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ModelResponse:
        """Rebuild a response from a journal record."""
        stop: StopReason = data.get("stop_reason", "end_turn")
        return cls(
            text=data.get("text"),
            tool_calls=[
                ToolCall.from_dict(c) for c in data.get("tool_calls", [])
            ],
            input_tokens=int(data.get("input_tokens", 0)),
            output_tokens=int(data.get("output_tokens", 0)),
            model=str(data.get("model", "unknown")),
            stop_reason=stop,
        )


@runtime_checkable
class ModelProvider(Protocol):
    """Anything that can take a conversation and return the next turn."""

    def complete(
        self,
        messages: list[Message],
        tools: list[ToolSpec],
    ) -> ModelResponse:
        """Produce the next assistant turn."""
        ...


class ScriptExhausted(RuntimeError):
    """A scripted model ran past the end of its script.

    Almost always means the loop took a path you did not plan for. That is
    a finding, not a nuisance: read the messages before extending the
    script.
    """


#: One scripted turn. A tool call, several parallel tool calls, a final
#: text answer, or a callable that inspects the conversation so far and
#: returns one of those. The callable form is how a script reacts to a tool
#: result without becoming a real model.
ScriptStep = (
    ToolCall
    | list[ToolCall]
    | str
    | Callable[[list["Message"]], "ToolCall | list[ToolCall] | str"]
)


def _count_assistant_turns(messages: Sequence[Message]) -> int:
    """How many turns the model has already taken in this conversation."""
    return sum(1 for m in messages if m.role == "assistant")


def _goal_of(messages: Sequence[Message]) -> str:
    """The first user message, which is the run's goal."""
    for message in messages:
        if message.role == "user":
            return message.content if isinstance(message.content, str) else ""
    return ""


class FakeModel:
    """A deterministic model scripted per goal.

    The script is a sequence of turns. Each turn is a
    :class:`~northstar_contracts.models.ToolCall` (one tool), a list of
    calls (parallel tools), a string (a final answer, which ends the run),
    or a callable taking the message list and returning one of those.

    The turn index comes from the conversation, not from an instance
    counter. That matters more than it looks: it makes the model a pure
    function of its input, so a journal replay produces byte-identical
    output and two runs of the same test cannot drift apart.

    Args:
        scripts: Goal to script. A goal matches exactly, or by
            case-insensitive substring, longest key first.
        default: Script used when no key matches.
        model: Model name reported in responses and spans.
        strict: Raise :class:`ScriptExhausted` when the loop takes more
            turns than the script has. Set ``False`` to end the run with a
            generic answer instead.

    Example:
        >>> model = FakeModel(default=[
        ...     ToolCall("c1", "get_order", {"order_id": "NR-2026-0041827"}),
        ...     "Your order was delivered on 11 July.",
        ... ])
        >>> model.complete([Message("user", "where is my order")], []).tool_calls
        [ToolCall(id='c1', name='get_order', arguments={'order_id': 'NR-2026-0041827'})]
    """

    def __init__(
        self,
        scripts: Mapping[str, Sequence[ScriptStep]] | None = None,
        default: Sequence[ScriptStep] | None = None,
        *,
        model: str = "fake-model-1",
        strict: bool = True,
    ) -> None:
        self.scripts: dict[str, list[ScriptStep]] = {
            k: list(v) for k, v in (scripts or {}).items()
        }
        self.default: list[ScriptStep] | None = (
            list(default) if default is not None else None
        )
        self.model = model
        self.strict = strict

    @classmethod
    def scripted(cls, *steps: ScriptStep, **kwargs: Any) -> FakeModel:
        """Build a model with a single default script."""
        return cls(default=list(steps), **kwargs)

    def script_for(self, goal: str) -> list[ScriptStep]:
        """Resolve the script for a goal.

        Raises:
            ScriptExhausted: If nothing matches and there is no default.
        """
        if goal in self.scripts:
            return self.scripts[goal]
        lowered = goal.lower()
        candidates = [k for k in self.scripts if k.lower() in lowered]
        if candidates:
            return self.scripts[max(candidates, key=len)]
        if self.default is not None:
            return self.default
        known = ", ".join(sorted(self.scripts)) or "(none)"
        raise ScriptExhausted(
            f"no script for goal {goal!r}; scripted goals: {known}"
        )

    def complete(
        self,
        messages: list[Message],
        tools: list[ToolSpec],
    ) -> ModelResponse:
        """Return the scripted turn for this point in the conversation."""
        turn = _count_assistant_turns(messages)
        script = self.script_for(_goal_of(messages))

        if turn >= len(script):
            if self.strict:
                raise ScriptExhausted(
                    f"script for this goal has {len(script)} turns, "
                    f"but the loop asked for turn {turn + 1}. The agent "
                    f"took a path the script does not cover."
                )
            return self._respond(
                messages, tools, "I have nothing further to add.", turn
            )

        step = script[turn]
        if callable(step) and not isinstance(step, ToolCall):
            step = step(messages)
        return self._respond(messages, tools, step, turn)

    def _respond(
        self,
        messages: Sequence[Message],
        tools: Sequence[ToolSpec],
        step: str | ToolCall | Sequence[ToolCall],
        turn: int,
    ) -> ModelResponse:
        """Turn one *resolved* script step into a :class:`ModelResponse`.

        Callable steps are resolved by :meth:`complete` before they get here,
        which is why this signature is narrower than :data:`ScriptStep`.
        """
        input_tokens = self._input_tokens(messages, tools)
        if isinstance(step, str):
            return ModelResponse(
                text=step,
                input_tokens=input_tokens,
                output_tokens=estimate_tokens(step),
                model=self.model,
                stop_reason="end_turn",
            )
        calls = [step] if isinstance(step, ToolCall) else list(step)
        calls = [
            ToolCall(
                id=c.id or f"call-{turn}-{i}",
                name=c.name,
                arguments=dict(c.arguments),
            )
            for i, c in enumerate(calls)
        ]
        return ModelResponse(
            tool_calls=calls,
            input_tokens=input_tokens,
            output_tokens=sum(estimate_tokens(c.to_dict()) for c in calls),
            model=self.model,
            stop_reason="tool_use",
        )

    @staticmethod
    def _input_tokens(
        messages: Sequence[Message],
        tools: Sequence[ToolSpec],
    ) -> int:
        """Deterministic prompt-size estimate, tool definitions included.

        Tool definitions are part of the prompt on every single turn. Teams
        are routinely surprised by this when a fifteen-tool agent's bill
        arrives, so the estimate counts them.
        """
        total = sum(estimate_tokens(m.content) + 3 for m in messages)
        total += sum(estimate_tokens(t.to_dict()) for t in tools)
        return total


class FlakyModel:
    """A seeded, probabilistic wrapper that misbehaves like a real model.

    Three failure modes, drawn from the multi-agent failure taxonomy in
    Chapter 16, because they are the ones that break loops in practice:

    * **repeat** — reissue the previous tool call verbatim (step
      repetition). Harmless with idempotent tools; expensive otherwise.
    * **stall** — burn a turn on text that does nothing.
    * **give up** — declare completion without doing the work (premature
      termination, and the reason outcome graders exist).

    Randomness is seeded per turn, from ``(seed, turn_index)``, not from a
    running generator. So this model is still a pure function of its input
    and still replays deterministically. A flaky model you cannot reproduce
    is a flaky test.

    Args:
        base: The provider used when no failure fires.
        seed: Reproducibility seed.
        p_repeat: Probability of repeating the last tool call.
        p_stall: Probability of a wasted turn.
        p_giveup: Probability of stopping early.
    """

    def __init__(
        self,
        base: ModelProvider,
        *,
        seed: int = 0,
        p_repeat: float = 0.0,
        p_stall: float = 0.0,
        p_giveup: float = 0.0,
    ) -> None:
        total = p_repeat + p_stall + p_giveup
        if not 0.0 <= total <= 1.0:
            raise ValueError(
                f"failure probabilities must sum to <= 1.0, got {total}"
            )
        self.base = base
        self.seed = seed
        self.p_repeat = p_repeat
        self.p_stall = p_stall
        self.p_giveup = p_giveup

    def complete(
        self,
        messages: list[Message],
        tools: list[ToolSpec],
    ) -> ModelResponse:
        """Return a possibly-degraded turn."""
        turn = _count_assistant_turns(messages)
        rng = random.Random(f"{self.seed}:{turn}")
        draw = rng.random()
        input_tokens = FakeModel._input_tokens(messages, tools)

        if draw < self.p_repeat:
            previous = self._last_tool_call(messages)
            if previous is not None:
                repeat = ToolCall(
                    id=f"call-{turn}-repeat",
                    name=previous.name,
                    arguments=dict(previous.arguments),
                )
                return ModelResponse(
                    tool_calls=[repeat],
                    input_tokens=input_tokens,
                    output_tokens=estimate_tokens(repeat.to_dict()),
                    model="flaky-model-1",
                    stop_reason="tool_use",
                )
        elif draw < self.p_repeat + self.p_stall:
            wasted = self._redundant_read(tools, turn)
            if wasted is not None:
                return ModelResponse(
                    text="Let me double-check the policy first.",
                    tool_calls=[wasted],
                    input_tokens=input_tokens,
                    output_tokens=estimate_tokens(wasted.to_dict()),
                    model="flaky-model-1",
                    stop_reason="tool_use",
                )
        elif draw < self.p_repeat + self.p_stall + self.p_giveup:
            text = "I have handled this for you."
            return ModelResponse(
                text=text,
                input_tokens=input_tokens,
                output_tokens=estimate_tokens(text),
                model="flaky-model-1",
                stop_reason="end_turn",
            )

        return self.base.complete(messages, tools)

    @staticmethod
    def _last_tool_call(messages: Sequence[Message]) -> ToolCall | None:
        """The most recent tool call anywhere in the conversation."""
        for message in reversed(messages):
            calls = message.tool_calls
            if calls:
                return calls[-1]
        return None

    @staticmethod
    def _redundant_read(
        tools: Sequence[ToolSpec],
        turn: int,
    ) -> ToolCall | None:
        """A pointless read call: the cheap, common form of wasted work.

        Only ever a read tool. A model that wastes a turn on a *write* is a
        different and much worse failure, and one this class will not
        simulate by accident.
        """
        for spec in tools:
            if not spec.writes and not spec.input_schema.get("required"):
                return ToolCall(f"call-{turn}-stall", spec.name, {})
        return None


class LiveModelUnavailable(RuntimeError):
    """A live provider was asked for and could not be used.

    Raised for a missing SDK or a missing key, with the exact command or
    variable needed. Never raised as a side effect of importing anything:
    a repository whose test suite fails because a key is absent has broken
    its own mock-mode promise.
    """


class LiveModel:
    """A real provider, imported lazily and never by default.

    Nothing in this class runs at import time. The provider SDK is imported
    inside :meth:`complete`, so a machine with no ``anthropic`` or
    ``openai`` package installed can still import ``northstar_runtime``,
    run the whole test suite, and work through every chapter.

    Install with the optional extra and set the key::

        pip install -e ".[live]"
        export ANTHROPIC_API_KEY=...

    Args:
        provider: ``"anthropic"`` or ``"openai"``.
        model: Model id. Defaults to a current model for the provider;
            pin it explicitly in production, and re-check it against the
            provider's model list, because these move.
        api_key_env: Environment variable holding the key.
        max_tokens: Output cap per turn.
        timeout: Per-request timeout in seconds.
    """

    _DEFAULT_MODELS = {
        "anthropic": "claude-sonnet-5",
        "openai": "gpt-5.1",
    }
    _DEFAULT_KEY_ENV = {
        "anthropic": "ANTHROPIC_API_KEY",
        "openai": "OPENAI_API_KEY",
    }

    def __init__(
        self,
        provider: str = "anthropic",
        model: str | None = None,
        *,
        api_key_env: str | None = None,
        max_tokens: int = 2048,
        timeout: float = 60.0,
    ) -> None:
        if provider not in self._DEFAULT_MODELS:
            known = ", ".join(sorted(self._DEFAULT_MODELS))
            raise ValueError(
                f"unknown provider {provider!r}; expected one of {known}"
            )
        self.provider = provider
        self.model = model or self._DEFAULT_MODELS[provider]
        self.api_key_env = api_key_env or self._DEFAULT_KEY_ENV[provider]
        self.max_tokens = max_tokens
        self.timeout = timeout

    def _api_key(self) -> str:
        """Read the key, or explain exactly what is missing."""
        key = os.environ.get(self.api_key_env)
        if not key:
            raise LiveModelUnavailable(
                f"{self.api_key_env} is not set. Mock mode needs no key: "
                f"use FakeModel. For a live run, export {self.api_key_env}."
            )
        return key

    def complete(
        self,
        messages: list[Message],
        tools: list[ToolSpec],
    ) -> ModelResponse:
        """Call the real provider and normalise the reply."""
        if self.provider == "anthropic":
            return self._complete_anthropic(messages, tools)
        return self._complete_openai(messages, tools)

    def _complete_anthropic(
        self,
        messages: list[Message],
        tools: list[ToolSpec],
    ) -> ModelResponse:
        try:
            import anthropic  # noqa: PLC0415
        except ImportError as exc:  # pragma: no cover - needs the SDK
            raise LiveModelUnavailable(
                "the anthropic package is not installed. "
                'Run: pip install -e ".[live]"'
            ) from exc

        client = anthropic.Anthropic(
            api_key=self._api_key(), timeout=self.timeout
        )
        system = "\n\n".join(
            str(m.content) for m in messages if m.role == "system"
        )
        reply = client.messages.create(
            model=self.model,
            max_tokens=self.max_tokens,
            system=system or anthropic.NOT_GIVEN,
            messages=[
                self._to_anthropic(m) for m in messages if m.role != "system"
            ],
            tools=[self._tool_schema(t) for t in tools] or anthropic.NOT_GIVEN,
        )

        text_parts: list[str] = []
        calls: list[ToolCall] = []
        for block in reply.content:
            if block.type == "text":
                text_parts.append(block.text)
            elif block.type == "tool_use":
                calls.append(
                    ToolCall(
                        id=block.id,
                        name=block.name,
                        arguments=dict(block.input or {}),
                    )
                )
        return ModelResponse(
            text="\n".join(text_parts) or None,
            tool_calls=calls,
            input_tokens=reply.usage.input_tokens,
            output_tokens=reply.usage.output_tokens,
            model=reply.model,
            stop_reason="tool_use" if calls else "end_turn",
        )

    def _complete_openai(
        self,
        messages: list[Message],
        tools: list[ToolSpec],
    ) -> ModelResponse:
        try:
            import openai  # noqa: PLC0415
        except ImportError as exc:  # pragma: no cover - needs the SDK
            raise LiveModelUnavailable(
                "the openai package is not installed. "
                'Run: pip install -e ".[live]"'
            ) from exc

        client = openai.OpenAI(api_key=self._api_key(), timeout=self.timeout)
        reply = client.chat.completions.create(
            model=self.model,
            max_completion_tokens=self.max_tokens,
            messages=[self._to_openai(m) for m in messages],
            tools=[
                {"type": "function", "function": self._tool_schema(t)}
                for t in tools
            ]
            or openai.NOT_GIVEN,
        )
        choice = reply.choices[0].message
        calls = [
            ToolCall(
                id=tc.id,
                name=tc.function.name,
                arguments=_loads_or_empty(tc.function.arguments),
            )
            for tc in (choice.tool_calls or [])
        ]
        usage = reply.usage
        return ModelResponse(
            text=choice.content,
            tool_calls=calls,
            input_tokens=usage.prompt_tokens if usage else 0,
            output_tokens=usage.completion_tokens if usage else 0,
            model=reply.model,
            stop_reason="tool_use" if calls else "end_turn",
        )

    @staticmethod
    def _tool_schema(spec: ToolSpec) -> dict[str, Any]:
        """Render a :class:`ToolSpec` as a provider tool definition."""
        return {
            "name": spec.name,
            "description": spec.description,
            "input_schema": spec.input_schema,
            "parameters": spec.input_schema,
        }

    @staticmethod
    def _to_anthropic(message: Message) -> dict[str, Any]:
        """Map one internal message onto the Anthropic wire format."""
        if message.role == "tool":
            payload = message.content
            return {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": payload.get("call_id", ""),
                        "content": str(payload.get("content", "")),
                        "is_error": not payload.get("ok", True),
                    }
                ],
            }
        return {"role": message.role, "content": message.content}

    @staticmethod
    def _to_openai(message: Message) -> dict[str, Any]:
        """Map one internal message onto the OpenAI wire format."""
        if message.role == "tool":
            payload = message.content
            return {
                "role": "tool",
                "tool_call_id": payload.get("call_id", ""),
                "content": str(payload.get("content", "")),
            }
        return {"role": message.role, "content": message.content}


def _loads_or_empty(raw: str | None) -> dict[str, Any]:
    """Parse tool arguments, tolerating an empty or malformed payload."""
    import json  # noqa: PLC0415

    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}
