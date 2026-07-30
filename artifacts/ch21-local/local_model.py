"""Pointing the loop at a local inference server. Opt-in, never default.

Local model serving is a separate concern from local agent development.
You can run the whole stack against a hosted model, and you can run a local
model behind a stack deployed in a cloud. The rule from the architecture:
**the inference server is not the agent runtime.** Ollama is not a workflow
engine, vLLM is not an authorization system, and neither one holds your
checkpoints.

Nothing in this module runs unless you ask for it. It imports no SDK, it
reads no environment variable at import time, and
:func:`local_provider` raises a clear error naming the variable rather
than falling back to anything.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

from northstar_runtime import LiveModel, LiveModelUnavailable, ModelProvider

__all__ = [
    "PROMOTION_CHECKS",
    "SERVERS",
    "Server",
    "local_provider",
    "unmet",
]


@dataclass(frozen=True)
class Server:
    """One local inference server, its role, and what to watch."""

    name: str
    best_local_role: str
    watch: str


#: Divided by role rather than by quality. The laptop tier is for
#: iteration; the GPU tier is what you scale on. Compatibility across all
#: of them is "OpenAI-like" rather than identical, and the differences land
#: exactly where an agent is sensitive.
SERVERS: tuple[Server, ...] = (
    Server("ollama", "laptop, quick model swaps",
           "concurrency, tool-call validity"),
    Server("llama.cpp", "CPU, Apple silicon, edge, GGUF",
           "chat template, hardware capacity"),
    Server("mlx", "Apple silicon development",
           "Apple-only; separate production controls"),
    Server("vllm", "GPU throughput, continuous batching",
           "tool parser is model-dependent"),
    Server("sglang", "structured generation",
           "model compatibility, observability"),
)

#: What to test before promoting a local model, in this order. Never
#: promote on text quality: an agent does not need prose, it needs valid
#: tool calls, and tool-call formatting depends on the chat template and
#: the tool parser as much as on the weights.
PROMOTION_CHECKS: tuple[str, ...] = (
    "json_and_schema_validity_under_real_schemas",
    "tool_selection_given_the_full_tool_list",
    "argument_accuracy_especially_ids_and_integer_cents",
    "parallel_tool_call_behaviour",
    "refusal_and_injection_behaviour",
    "effective_context_length_under_real_prompts",
    "throughput_and_p95_latency_at_real_concurrency",
    "memory_and_kv_cache_pressure",
    "repeat_reliability_pass_k",
    "fallback_when_the_endpoint_is_saturated",
)


def unmet(results: dict[str, bool]) -> list[str]:
    """Promotion checks that have not been run or have not passed.

    A check nobody ran is not a check that passed, so an absent key counts
    against you. This is the function a promotion decision calls, and the
    reason it takes a dict rather than a score: task success and argument
    accuracy are separate measurements, and averaging them hides the one
    that matters.
    """
    return [check for check in PROMOTION_CHECKS if not results.get(check)]


def local_provider(env: dict[str, str] | None = None) -> ModelProvider:
    """Point :class:`LiveModel` at a local OpenAI-compatible server.

    ``LiveModel`` speaks the OpenAI-compatible chat and tools API, so
    pointing it at a local server is a base-URL change rather than a code
    change. That is the payoff for keeping the model behind a provider
    protocol: swapping inference is configuration, and swapping it does not
    put a single invariant at risk.

    Raises:
        LiveModelUnavailable: When the base URL or the model name is not
            set. Naming the variable beats a default that silently reaches
            a hosted endpoint.
    """
    environ = env if env is not None else dict(os.environ)
    base_url = environ.get("NORTHSTAR_MODEL_BASE_URL")
    model = environ.get("NORTHSTAR_MODEL_NAME")
    if not base_url or not model:
        raise LiveModelUnavailable(
            "local inference needs NORTHSTAR_MODEL_BASE_URL and "
            "NORTHSTAR_MODEL_NAME. Mock mode needs neither: leave "
            "MODEL_MODE unset."
        )
    return _configured(base_url, model, environ)


def _configured(
    base_url: str,
    model: str,
    environ: dict[str, str],
) -> ModelProvider:
    """Build the provider. Separated so the wiring is readable.

    ``LiveModel`` takes its base URL from the SDK client it builds, so the
    variables are set on the environment it reads rather than passed as
    constructor arguments. That is the whole overlay: three variables, no
    code change.
    """
    api_key_env = "NORTHSTAR_MODEL_API_KEY"
    os.environ.setdefault(api_key_env, environ.get(api_key_env, "unused"))
    os.environ.setdefault("OPENAI_BASE_URL", base_url)
    return LiveModel("openai", model, api_key_env=api_key_env)


def describe() -> list[dict[str, Any]]:
    """The server table, for the demo's output."""
    return [
        {"server": s.name, "role": s.best_local_role, "watch": s.watch}
        for s in SERVERS
    ]
