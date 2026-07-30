"""Everything that can change behaviour, hashed into one value.

The first discipline in the chapter is a list. Code, prompt, model
snapshot, tool schema *and* implementation, protocol servers, policy,
memory configuration, guardrails, judge, dataset, sandbox image, dependency
lock, and flags are all release artifacts, and their combined hash belongs
on every run.

:func:`effective_config_hash` is that value. Its usefulness is entirely in
being *complete*: a hash over the code alone answers the wrong question at
03:12, because the thing that changed was the prompt.

The four agent versions below are what the rest of the artifact releases
against. They differ in exactly two dimensions, and both are behavioural:

* how often the model drifts — repeats a call, burns a turn, or declares
  completion without doing the work. This stands in for the prompt edit
  that made an agent give up more readily, which is the regression the
  reliability gate exists to catch. In mock mode the deployed version has
  no drift at all, so its ``pass^k`` is exactly 1.0; that is a property of
  a scripted model rather than a claim about a real one, and the gate
  arithmetic is identical either way.
* whether the trajectory holds its invariants — reading the policy before
  moving money, and stamping a derived idempotency key on every write.
  That is the regression the trajectory gate exists to catch.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from northstar_contracts import ToolSpec, content_hash, short_hash

__all__ = [
    "CANDIDATES",
    "RELEASE_ARTIFACTS",
    "AgentVersion",
    "V8",
    "V9_GENEROUS",
    "V9_GOOD",
    "V9_MARGINAL",
    "V9_REGRESSED",
    "V9_UNSAFE",
    "effective_config_hash",
    "version_named",
]

#: The list from the chapter, in the order it prints them. Anything here
#: that is not in your configuration hash is a question you cannot answer
#: during an incident.
RELEASE_ARTIFACTS: tuple[str, ...] = (
    "agent code",
    "model snapshot",
    "system and developer prompts",
    "tool schemas",
    "tool implementations",
    "MCP and A2A servers",
    "policy",
    "memory configuration",
    "guardrails",
    "judge and evaluation dataset",
    "sandbox image",
    "dependency lock",
    "feature flags",
)

#: A snapshot, never a floating alias. The mock provider's name carries a
#: date for the same reason a real one should.
MODEL_SNAPSHOT = "fake-model-1-2026-07-01"
SANDBOX_IMAGE_DIGEST = "sha256:0f1e2d3c4b5a69788796a5b4c3d2e1f0"
POLICY_DIGEST = "northstar-refund-policy-2026-07-01"
GUARDRAIL_DIGEST = "northstar-guardrails-2026-06-12"


def effective_config_hash(
    *,
    agent: str,
    model: str,
    prompt: str,
    tools: Sequence[ToolSpec],
    policy: str = POLICY_DIGEST,
    guardrails: str = GUARDRAIL_DIGEST,
    sandbox: str = SANDBOX_IMAGE_DIGEST,
) -> str:
    """Hash every behavioural artifact into the value a run carries.

    Tool *versions* are hashed rather than the whole schema, because a
    schema that is reformatted has not changed behaviour and a version
    bump is the team's explicit statement that it has. The implementation
    is versioned alongside it — the schema is what the model sees, the
    implementation is what happens, and they frequently live in different
    repositories.
    """
    return content_hash(
        {
            "agent": agent,
            "model": model,
            "prompt": content_hash(prompt),
            "tools": {spec.name: spec.version for spec in tools},
            "policy": policy,
            "guardrails": guardrails,
            "sandbox": sandbox,
        }
    )


@dataclass(frozen=True)
class AgentVersion:
    """One releasable agent, and the two ways it can differ behaviourally.

    Attributes:
        name: The version tag a run is pinned to at admission.
        system_prompt: Prompt text. Part of the configuration hash, so an
            edit is a release whether or not anyone called it one.
        p_repeat: Probability of reissuing the previous call.
        p_stall: Probability of burning a turn.
        p_giveup: Probability of declaring success without doing the work.
        behaviour: Which trajectory this version takes. ``"correct"``
            reads the policy before moving money; ``"unsafe"`` does not;
            ``"generous"`` reads it and then refunds more than the item is
            worth. Scenarios supply one script per behaviour.
        stamps_idempotency_key: Whether writes carry a key derived from
            the run and the step.
        note: What this version is here to demonstrate.
    """

    name: str
    system_prompt: str
    p_repeat: float = 0.0
    p_stall: float = 0.0
    p_giveup: float = 0.0
    behaviour: str = "correct"
    stamps_idempotency_key: bool = True
    note: str = ""

    @property
    def reads_policy_first(self) -> bool:
        """Whether this version reads the refund policy before it pays."""
        return self.behaviour != "unsafe"

    @property
    def drift(self) -> float:
        """Total probability that a turn goes wrong somehow."""
        return self.p_repeat + self.p_stall + self.p_giveup

    def config_hash(self, tools: Sequence[ToolSpec]) -> str:
        """The effective configuration hash for this version."""
        return effective_config_hash(
            agent=self.name,
            model=MODEL_SNAPSHOT,
            prompt=self.system_prompt,
            tools=tools,
        )

    def short_config_hash(self, tools: Sequence[ToolSpec]) -> str:
        """The first twelve characters, which is what a log line carries."""
        return self.config_hash(tools)[:12]


_BASE_PROMPT = """\
You are the Northstar Returns support agent.

Read the order and the refund policy before you quote a figure or move
money. Amounts are always integer cents. Escalate anything flagged for
fraud review rather than deciding it yourself."""

_SOFTENED_PROMPT = """\
You are the Northstar Returns support agent.

Be helpful and concise. Resolve the customer's problem in as few steps as
you can, and wrap up once you believe the issue is handled."""

V8 = AgentVersion(
    name="v8",
    system_prompt=_BASE_PROMPT,
    note="the deployed baseline the stored baseline file describes",
)

V9_GOOD = AgentVersion(
    name="v9-good",
    system_prompt=_BASE_PROMPT + "\n\nPrefer one message over two.",
    note="a prompt edit that changes the hash and not the behaviour",
)

V9_MARGINAL = AgentVersion(
    name="v9-marginal",
    system_prompt=_SOFTENED_PROMPT,
    p_giveup=0.08,
    note="clears an absolute floor, fails against the baseline",
)

V9_REGRESSED = AgentVersion(
    name="v9-regressed",
    system_prompt=_SOFTENED_PROMPT + "\n\nWrap up as soon as you can.",
    p_giveup=0.35,
    note="the softened prompt, and the regression the gate must block",
)

V9_UNSAFE = AgentVersion(
    name="v9-unsafe",
    system_prompt=_BASE_PROMPT,
    behaviour="unsafe",
    stamps_idempotency_key=False,
    note="reliability is unchanged; two trajectory invariants are broken",
)

V9_GENEROUS = AgentVersion(
    name="v9-generous",
    system_prompt=_BASE_PROMPT + "\n\nErr on the side of the customer.",
    behaviour="generous",
    note="same trajectory, different decision; what shadow traffic is for",
)

#: Everything a release might be asked to gate, by tag.
CANDIDATES: dict[str, AgentVersion] = {
    v.name: v
    for v in (
        V8,
        V9_GOOD,
        V9_MARGINAL,
        V9_REGRESSED,
        V9_UNSAFE,
        V9_GENEROUS,
    )
}


def version_named(name: str) -> AgentVersion:
    """Look up a version by tag.

    Raises:
        KeyError: With the known tags listed, because a gate invoked with
            a version that does not exist should say so rather than
            silently grading the wrong thing.
    """
    try:
        return CANDIDATES[name]
    except KeyError:
        known = ", ".join(sorted(CANDIDATES))
        raise KeyError(
            f"unknown agent version {name!r}; known versions: {known}"
        ) from None


def digest_of(value: object) -> str:
    """A short, stable digest for anything JSON-serialisable."""
    return short_hash(value)
