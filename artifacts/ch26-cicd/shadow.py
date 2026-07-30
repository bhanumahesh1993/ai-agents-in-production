"""The shadow adapter: run the candidate on real input, change nothing.

Shadow traffic is the stage that catches what curated evaluation cannot,
because it runs against real input diversity. The whole trick is that the
candidate must be allowed to *decide* while being prevented from *acting*.

So writes are recorded rather than dropped. A dropped write tells you the
run finished; a recorded write intent tells you what the candidate would
have done to a customer's money, which is the comparison you actually
want. Two versions can then be diffed on their decisions.

The adapter subclasses :class:`ToolRegistry` rather than wrapping one,
because the loop takes a registry. The dispatch body is otherwise exactly
the chapter's excerpt.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from deployment import Scenario, build_loop, build_model
from northstar_contracts import ToolCall, ToolResult, World, canonical_json
from northstar_runtime import ToolRegistry
from versions import AgentVersion

__all__ = [
    "ShadowAdapter",
    "ShadowComparison",
    "ShadowRun",
    "compare",
    "shadow_run",
]


class ShadowAdapter(ToolRegistry):
    """Run the candidate on real input. Writes go nowhere, on purpose."""

    def __init__(self, registry: ToolRegistry) -> None:
        super().__init__(
            inject_idempotency_key=False, validate=registry.validate
        )
        self.register_all(registry.bindings())
        self.intents: list[ToolCall] = []
        self.reads = 0

    def dispatch(
        self,
        call: ToolCall,
        run_id: str | None = None,
        step: int | None = None,
    ) -> ToolResult:
        """Reads pass through. Writes are recorded and never executed."""
        spec = self.spec_for(call.name)
        if spec is not None and spec.writes:
            self.intents.append(call)          # recorded, never executed
            return ToolResult(call.id, ok=True, content={"shadow": True})
        self.reads += 1
        return super().dispatch(call, run_id=run_id, step=step)

    def decisions(self) -> list[dict[str, Any]]:
        """The recorded intents, normalised for comparison.

        The idempotency key is dropped: it is derived from the run id, so
        two versions running the same ticket always differ on it, and a
        diff that flags that difference flags every run.
        """
        return [
            {
                "tool": call.name,
                "arguments": {
                    k: v
                    for k, v in sorted(call.arguments.items())
                    if k != "idempotency_key"
                },
            }
            for call in self.intents
        ]


@dataclass
class ShadowRun:
    """One shadowed run: what it decided, and proof it did nothing."""

    version: str
    scenario: str
    adapter: ShadowAdapter
    world: World

    @property
    def side_effects(self) -> int:
        """Entries in the authoritative ledger. Must be zero."""
        return len(self.world.ledger)

    @property
    def write_intents(self) -> int:
        """Writes the candidate would have made."""
        return len(self.adapter.intents)


def shadow_run(
    version: AgentVersion,
    scenario: Scenario,
    *,
    run_id: str = "",
) -> ShadowRun:
    """Run one scenario in shadow: real reads, recorded writes, no effects."""
    world = World()
    base = ToolRegistry().register_all(world.tools())
    adapter = ShadowAdapter(base)
    model = build_model(version, scenario, 0, deterministic=True)
    loop = build_loop(version, model, adapter)
    loop.run(
        scenario.goal,
        run_id=run_id or f"shadow_{version.name}_{scenario.name}",
    )
    return ShadowRun(
        version=version.name,
        scenario=scenario.name,
        adapter=adapter,
        world=world,
    )


@dataclass(frozen=True)
class ShadowComparison:
    """What two versions would have done differently."""

    scenario: str
    baseline: str
    candidate: str
    only_baseline: list[dict[str, Any]]
    only_candidate: list[dict[str, Any]]
    common: int

    @property
    def identical(self) -> bool:
        """Whether the two versions would have made the same decisions."""
        return not self.only_baseline and not self.only_candidate

    def report(self) -> list[str]:
        """The lines a shadow diff should print."""
        if self.identical:
            return [
                f"{self.scenario}: identical, {self.common} write "
                f"intent(s) either way"
            ]
        lines = [f"{self.scenario}: decisions differ"]
        for decision in self.only_baseline:
            lines.append(f"  only {self.baseline}: {canonical_json(decision)}")
        for decision in self.only_candidate:
            lines.append(f"  only {self.candidate}: {canonical_json(decision)}")
        return lines


def compare(baseline: ShadowRun, candidate: ShadowRun) -> ShadowComparison:
    """Diff two shadowed runs on the decisions they would have made."""
    left = [canonical_json(d) for d in baseline.adapter.decisions()]
    right = [canonical_json(d) for d in candidate.adapter.decisions()]
    only_left = [d for d in left if d not in right]
    only_right = [d for d in right if d not in left]
    return ShadowComparison(
        scenario=candidate.scenario,
        baseline=baseline.version,
        candidate=candidate.version,
        only_baseline=[
            d for d in baseline.adapter.decisions()
            if canonical_json(d) in only_left
        ],
        only_candidate=[
            d for d in candidate.adapter.decisions()
            if canonical_json(d) in only_right
        ],
        common=len(left) - len(only_left),
    )
