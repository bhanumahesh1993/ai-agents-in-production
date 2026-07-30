"""The four-way harness: baseline, cached, routed, and cached-plus-routed.

Every number this module reports is computed from a run that actually
happened. Tokens come from the model responses, cents come from
``northstar_telemetry.CostLedger`` at the illustrative prices in
``budgets.py``, successes come from a state grader reading the
authoritative world, and escalations come from the deterministic check in
``router.py`` firing.

Latency is the one figure that is *modelled* rather than measured, and it
says so. A mock model returns in microseconds, so wall-clock timing here
would report the speed of Python rather than the shape of an agent turn.
:data:`MODEL_LATENCY_MS` and :data:`TOOL_LATENCY_MS` are the declared
table the modelled figure is computed from; replace them with your own
measurements before quoting a p95 anywhere.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass, field

from budgets import (
    CACHE_READ_MULTIPLIER,
    HUMAN_HANDLING_CENTS,
    ILLUSTRATIVE_PRICES,
    LARGE_MODEL,
    SMALL_MODEL,
    budgeted_loop,
    to_cents,
)
from cache import PrefixCache, prefix_of
from northstar_contracts import (
    Message,
    Money,
    RunState,
    ToolResult,
    ToolSpec,
    World,
)
from northstar_evals import GradeResult
from northstar_policy import (
    BudgetExceeded,
    Principal,
    default_northstar_policy,
)
from northstar_runtime import (
    AgentLoop,
    FakeModel,
    ModelResponse,
    PolicyDenied,
    ToolRegistry,
)
from northstar_telemetry import CostLedger
from northstar_telemetry.cost import NANOCENTS
from router import escalate, route
from scenarios import SCENARIOS, Scenario

__all__ = [
    "CONFIGS",
    "Config",
    "ConfigReport",
    "MODEL_LATENCY_MS",
    "RunResult",
    "TOOL_LATENCY_MS",
    "measure",
    "measure_all",
    "run_scenario",
]

#: Declared latency model, in milliseconds. Not measured here; see the
#: module docstring. ``base`` is time to first token, the per-token figures
#: are generation and prompt-processing rates.
MODEL_LATENCY_MS: dict[str, dict[str, float]] = {
    SMALL_MODEL: {"base": 120.0, "per_output_token": 0.30},
    LARGE_MODEL: {"base": 450.0, "per_output_token": 0.90},
}
UNCACHED_INPUT_MS_PER_TOKEN = 0.05
CACHED_INPUT_MS_PER_TOKEN = 0.005

#: A write crosses a payments boundary; a read hits a cache in front of a
#: database. The gap is why tool latency, not token generation, usually
#: dominates an agent's end-to-end time.
TOOL_LATENCY_MS: dict[str, float] = {"read": 40.0, "write": 220.0}

TENANT = "acme-support"


@dataclass(frozen=True)
class Config:
    """One row of the comparison table."""

    label: str
    cached: bool
    routed: bool


#: The four configurations the chapter compares.
CONFIGS: tuple[Config, ...] = (
    Config("baseline", cached=False, routed=False),
    Config("cached", cached=True, routed=False),
    Config("routed", cached=False, routed=True),
    Config("cached+routed", cached=True, routed=True),
)


@dataclass
class CallRecord:
    """One model call, priced and timed.

    Cost is carried in nanocents, not cents. Rounding every call up to a
    whole cent turns a table of real differences into a table of ones:
    every call in mock mode costs a fraction of a cent, so the rounding
    *is* the number. Accumulate exactly, round once at the edge — which is
    what ``CostLedger`` does and why this record stores its output.
    """

    turn: int
    model: str
    input_tokens: int
    cached_tokens: int
    output_tokens: int
    billed_input_tokens: int
    nanocents: int
    latency_ms: float

    @property
    def cents(self) -> Money:
        """This one call, rounded up. For display, never for a total."""
        return to_cents(self.nanocents)


class MeteredModel:
    """Routes each turn, prices what it costs, and records the receipt.

    The loop sees one :class:`ModelProvider`. Behind it sit two scripted
    models, an optional prefix cache, and a price table. Everything the
    comparison reports about model spend is accumulated here, at the only
    place in the system that knows both which model ran and how many
    tokens it read.

    Args:
        scenario: Supplies both scripts and the per-turn step kinds.
        routed: Ask :func:`router.route` which model serves each turn.
            When ``False`` every turn goes to the large model.
        cache: Prefix cache, or ``None`` for no caching.
        tenant: Cache scope. Never omit it.
    """

    def __init__(
        self,
        scenario: Scenario,
        *,
        routed: bool,
        cache: PrefixCache | None,
        tenant: str = TENANT,
        run_id: str = "",
    ) -> None:
        self.scenario = scenario
        self.routed = routed
        self.cache = cache
        self.tenant = tenant
        self.run_id = run_id
        self.ledger = CostLedger(ILLUSTRATIVE_PRICES, strict=True)
        self.small = FakeModel(
            default=list(scenario.small), model=SMALL_MODEL
        )
        self.large = FakeModel(
            default=list(scenario.large), model=LARGE_MODEL
        )
        self.force_large: set[int] = set()
        self.records: list[CallRecord] = []
        self.escalations = 0

    # ------------------------------------------------------------- routing

    def class_of(self, turn: int) -> str:
        """Which capability class serves ``turn``: ``small`` or ``large``."""
        if not self.routed or turn in self.force_large:
            return "large"
        kinds = self.scenario.step_kinds
        kind = kinds[turn] if turn < len(kinds) else "plan"
        return route(kind)

    def complete(
        self,
        messages: list[Message],
        tools: list[ToolSpec],
    ) -> ModelResponse:
        """Serve one turn from the routed model, metered and priced."""
        turn = sum(1 for m in messages if m.role == "assistant")
        klass = self.class_of(turn)
        provider = self.small if klass == "small" else self.large
        response = provider.complete(messages, tools)

        cached = 0
        if self.cache is not None:
            cached = min(
                self.cache.lookup(self.tenant, prefix_of(messages, tools)),
                response.input_tokens,
            )
        uncached = response.input_tokens - cached
        billed_input = uncached + int(cached * CACHE_READ_MULTIPLIER)
        self.ledger.record(
            response.model,
            billed_input,
            response.output_tokens,
            run_id=self.run_id,
        )

        latency = MODEL_LATENCY_MS[response.model]
        self.records.append(
            CallRecord(
                turn=turn,
                model=response.model,
                input_tokens=response.input_tokens,
                cached_tokens=cached,
                output_tokens=response.output_tokens,
                billed_input_tokens=billed_input,
                nanocents=self.ledger.entries[-1].nanocents,
                latency_ms=(
                    latency["base"]
                    + response.output_tokens * latency["per_output_token"]
                    + uncached * UNCACHED_INPUT_MS_PER_TOKEN
                    + cached * CACHED_INPUT_MS_PER_TOKEN
                ),
            )
        )
        return response


@dataclass
class RunResult:
    """What one scenario cost and whether the world ended up right."""

    scenario: str
    passed: bool
    grade: GradeResult
    calls: list[CallRecord]
    escalations: int
    tool_latency_ms: float
    status: str
    notes: list[str] = field(default_factory=list)

    @property
    def model_nanocents(self) -> int:
        """Exact model spend for this run."""
        return sum(c.nanocents for c in self.calls)

    @property
    def model_cents(self) -> Money:
        """Model spend for this run, rounded up once."""
        return to_cents(self.model_nanocents)

    @property
    def latency_ms(self) -> float:
        """Modelled end-to-end latency: model time plus tool time."""
        return sum(c.latency_ms for c in self.calls) + self.tool_latency_ms


def _assistant_turns(messages: Sequence[Message]) -> int:
    """How many turns the model has already taken."""
    return sum(1 for m in messages if m.role == "assistant")


def _results_added(before: RunState, after: RunState) -> list[ToolResult]:
    """The tool observations one step appended, as results again."""
    out: list[ToolResult] = []
    for message in after.messages[len(before.messages):]:
        if message.role != "tool" or not isinstance(message.content, dict):
            continue
        payload = message.content
        out.append(
            ToolResult(
                call_id=str(payload.get("call_id", "")),
                ok=bool(payload.get("ok")),
                content=payload.get("content"),
                truncated=bool(payload.get("truncated", False)),
            )
        )
    return out


def _tool_latency(
    before: RunState,
    after: RunState,
    tools: ToolRegistry,
) -> float:
    """Modelled tool time for the calls one step made."""
    total = 0.0
    for message in after.messages[len(before.messages):]:
        if message.role != "tool" or not isinstance(message.content, dict):
            continue
        spec = tools.spec_for(str(message.content.get("tool", "")))
        writes = bool(spec and spec.writes)
        total += TOOL_LATENCY_MS["write" if writes else "read"]
    return total


def _build(
    scenario: Scenario,
    config: Config,
    cache: PrefixCache | None,
) -> tuple[World, ToolRegistry, MeteredModel, AgentLoop]:
    """Wire one run: a fresh world, the policy, and a metered model."""
    world = World()
    tools = ToolRegistry(inject_idempotency_key=True).register_all(
        world.tools()
    )
    model = MeteredModel(
        scenario,
        routed=config.routed,
        cache=cache,
        run_id=f"run_ch25_{config.label.replace('+', '_')}_{scenario.name}",
    )
    loop = budgeted_loop(
        model,
        tools,
        policy=default_northstar_policy(),
        principal=Principal.of("CUST-8841", "orders:read", "refunds:write"),
    )
    return world, tools, model, loop


def run_scenario(
    scenario: Scenario,
    config: Config,
    cache: PrefixCache | None = None,
) -> RunResult:
    """Run one scenario under one configuration and grade the world.

    The loop is stepped by hand rather than driven with ``resume`` so the
    deterministic check in ``router.py`` can look at each turn's results
    before the next turn is taken. That is what a cascade is: verify, then
    decide whether the cheap answer stands.

    A step that already changed the world is **never** redone. Re-running
    a committed mutation is not an escalation, it is a duplicate, and the
    harness records the refusal instead.
    """
    world, tools, model, loop = _build(scenario, config, cache)
    run_id = model.run_id
    notes: list[str] = []
    tool_ms = 0.0
    state = loop.start(scenario.goal, run_id=run_id)
    try:
        while state.status == "running":
            turn = _assistant_turns(state.messages)
            effects_before = len(world.ledger)
            nxt = loop.step(state)
            tool_ms += _tool_latency(state, nxt, tools)

            if model.class_of(turn) == "small":
                failed = [
                    r
                    for r in _results_added(state, nxt)
                    if escalate(r, world)
                ]
                if failed and len(world.ledger) != effects_before:
                    notes.append(
                        f"turn {turn}: check failed after the world changed; "
                        f"escalation refused, this is a compensation case"
                    )
                elif failed:
                    model.force_large.add(turn)
                    model.escalations += 1
                    notes.append(
                        f"turn {turn}: {failed[0].error or 'check failed'}"
                        f" -> redone on {LARGE_MODEL}"
                    )
                    nxt = loop.step(state)
                    tool_ms += _tool_latency(state, nxt, tools)
            state = nxt
    except (BudgetExceeded, PolicyDenied) as exc:
        notes.append(f"{type(exc).__name__}: {exc}")
        state = state.with_status("failed")

    grade = scenario.grader.grade(state, world)
    return RunResult(
        scenario=scenario.name,
        passed=grade.passed,
        grade=grade,
        calls=model.records,
        escalations=model.escalations,
        tool_latency_ms=tool_ms,
        status=state.status,
        notes=notes,
    )


@dataclass(frozen=True)
class ConfigReport:
    """The aggregate for one configuration. Every field is computed."""

    label: str
    runs: list[RunResult]
    cache: PrefixCache | None

    @property
    def successes(self) -> int:
        """Runs a state grader confirmed. Not runs that returned 200."""
        return sum(1 for r in self.runs if r.passed)

    @property
    def verified_success_rate(self) -> float:
        """Graded successes over completed runs."""
        return self.successes / len(self.runs) if self.runs else 0.0

    @property
    def model_calls(self) -> int:
        """Model calls across the whole task set."""
        return sum(len(r.calls) for r in self.runs)

    @property
    def input_tokens(self) -> int:
        """Prompt tokens read, cached and uncached."""
        return sum(c.input_tokens for r in self.runs for c in r.calls)

    @property
    def cached_tokens(self) -> int:
        """Prompt tokens served from the prefix cache."""
        return sum(c.cached_tokens for r in self.runs for c in r.calls)

    @property
    def output_tokens(self) -> int:
        """Completion tokens generated."""
        return sum(c.output_tokens for r in self.runs for c in r.calls)

    @property
    def escalations(self) -> int:
        """Turns the deterministic check sent back to the large model."""
        return sum(r.escalations for r in self.runs)

    @property
    def model_nanocents(self) -> int:
        """Exact model spend across the task set."""
        return sum(r.model_nanocents for r in self.runs)

    @property
    def model_cents(self) -> Money:
        """The invoice line: model spend only, rounded up once."""
        return to_cents(self.model_nanocents)

    @property
    def human_cents(self) -> Money:
        """What the failures cost once a person picks them up."""
        return (len(self.runs) - self.successes) * HUMAN_HANDLING_CENTS

    @property
    def total_cents(self) -> Money:
        """Model spend plus human handling. The honest numerator."""
        return self.model_cents + self.human_cents

    @property
    def cents_per_call(self) -> float:
        """Model spend per model call. The number an invoice shows."""
        if not self.model_calls:
            return 0.0
        return self.model_nanocents / NANOCENTS / self.model_calls

    @property
    def cents_per_success(self) -> float:
        """Total cost per *graded* success. The number that decides."""
        if not self.successes:
            return math.inf
        exact = self.model_nanocents / NANOCENTS + self.human_cents
        return exact / self.successes

    @property
    def latencies_ms(self) -> list[float]:
        """Modelled end-to-end latency, one figure per run."""
        return sorted(r.latency_ms for r in self.runs)

    def percentile_ms(self, fraction: float) -> float:
        """Nearest-rank percentile of the modelled run latencies."""
        ordered = self.latencies_ms
        if not ordered:
            return 0.0
        index = max(0, math.ceil(fraction * len(ordered)) - 1)
        return ordered[index]

    @property
    def p50_ms(self) -> float:
        """Median modelled latency."""
        return self.percentile_ms(0.50)

    @property
    def p95_ms(self) -> float:
        """Ninety-fifth percentile modelled latency."""
        return self.percentile_ms(0.95)

    @property
    def cache_hit_rate(self) -> float:
        """Fraction of model calls served a warm prefix."""
        return self.cache.hit_rate if self.cache else 0.0

    def to_dict(self) -> dict[str, object]:
        """A dashboard-shaped summary of this configuration."""
        return {
            "config": self.label,
            "runs": len(self.runs),
            "verified_successes": self.successes,
            "verified_success_rate": round(self.verified_success_rate, 4),
            "model_calls": self.model_calls,
            "input_tokens": self.input_tokens,
            "cached_tokens": self.cached_tokens,
            "output_tokens": self.output_tokens,
            "cache_hit_rate": round(self.cache_hit_rate, 4),
            "escalations": self.escalations,
            "model_cents": self.model_cents,
            "human_cents": self.human_cents,
            "cents_per_call": round(self.cents_per_call, 4),
            "cents_per_success": round(self.cents_per_success, 4),
            "p50_latency_ms": round(self.p50_ms, 1),
            "p95_latency_ms": round(self.p95_ms, 1),
            "prices_are_illustrative": True,
        }


def measure(
    config: Config,
    scenarios: Sequence[Scenario] = SCENARIOS,
) -> ConfigReport:
    """Run the whole task set under one configuration."""
    cache = PrefixCache() if config.cached else None
    runs = [run_scenario(s, config, cache) for s in scenarios]
    return ConfigReport(label=config.label, runs=runs, cache=cache)


def measure_all(
    scenarios: Sequence[Scenario] = SCENARIOS,
) -> list[ConfigReport]:
    """Run the task set under all four configurations."""
    return [measure(c, scenarios) for c in CONFIGS]
