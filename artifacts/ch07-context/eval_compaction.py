"""Measuring what compaction did to answer quality, on a fixed task set.

Token reduction is trivially maximised by discarding everything, so the
only measurement that means anything is outcome quality on a fixed task
set, graded against authoritative state, before and after.

Three configurations, one task set, twelve tasks:

``none``
    No compaction. The window grows, and the longest sessions exhaust the
    run's cost budget before they finish.
``naive``
    A good summariser, a correct boundary, a step-span pointer, and no
    pinned block. It holds the token ceiling and reproduces the duplicate
    refund at a measurable rate.
``pinned``
    The same compactor with the computed facts prepended. Same ceiling,
    duplicate rate zero.

The twelve tasks differ only in how long the customer wanders before
asking about the refund again. That is deliberate: every failure in this
chapter needs a full window to appear, so a task set of uniformly short
runs would report that all three configurations are equally good.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from budget import ContextBudget, account, total_tokens
from compact import Summarizer, compact, naive_compact
from northstar_contracts import EventLog, Message, RunState, ToolSpec, World
from northstar_evals import (
    ReliabilityReport,
    StateGrader,
    TrajectoryGrader,
    grade_all,
    run_repeated,
)
from northstar_policy import Principal, default_northstar_policy
from northstar_runtime import (
    AgentLoop,
    MemoryCheckpointer,
    ModelResponse,
)
from session import (
    AMOUNT,
    GOAL,
    ORDER,
    ScriptedSession,
    long_session,
    northstar_tools,
)

__all__ = [
    "BUDGET",
    "budget_report",
    "MODES",
    "TASKS",
    "CompactingModel",
    "RunOutcome",
    "compare",
    "measure",
    "run_one",
    "with_compaction",
]

#: The caps this chapter runs under. Small next to a real window on
#: purpose: the arithmetic of the failure does not change with the
#: absolute numbers, and a test suite that has to build a 60,000-token
#: session to see the bug is a test suite nobody runs.
BUDGET = ContextBudget(
    total=6_000,
    system=800,
    tools=3_000,
    pinned=400,
    history=1_600,
    retrieved=900,
    reserve=600,
)

#: The three configurations, in the order the demo prints them.
MODES: tuple[str, ...] = ("none", "naive", "pinned")

#: Twelve variants of the same session, differing in filler rounds.
TASKS: tuple[int, ...] = tuple(range(1, 13))

#: A cost ceiling that a compacted run clears and an uncompacted one does
#: not. Every token in context is re-sent on every subsequent turn, so an
#: uncompacted long session pays for its history a linear number of times
#: and the total is quadratic.
BUDGET_CENTS = 85


class CompactingModel:
    """Compaction middleware, wrapped around a model provider.

    The loop assembles its own message list and hands it to the provider,
    so the honest place to put middleware that rewrites that list is in
    front of the provider. Nothing in :class:`AgentLoop` changes.

    Args:
        inner: The provider actually being called.
        budget: Caps to enforce.
        events: The run's event log, which is where the pinned block is
            computed from. Passing it in rather than reaching for a global
            is what keeps the pinned block a projection of one run.
        pinned: ``False`` selects :func:`naive_compact`, which is the
            same compactor with the computed facts removed.
    """

    def __init__(
        self,
        inner: Any,
        budget: ContextBudget,
        events: EventLog,
        *,
        pinned: bool = True,
    ) -> None:
        self.inner = inner
        self.budget = budget
        self.events = events
        self.pinned = pinned
        self.summarizer = Summarizer()
        self.model = getattr(inner, "model", "unknown")
        #: How many times the middleware actually rewrote the list.
        self.compactions = 0
        #: Peak assembled context, in estimated tokens.
        self.peak_tokens = 0

    def complete(
        self,
        messages: list[Message],
        tools: list[ToolSpec],
    ) -> ModelResponse:
        """Compact, then delegate. Idempotent when already under budget."""
        state = RunState(run_id="compaction-middleware", messages=messages)
        fn = compact if self.pinned else naive_compact
        rewritten = fn(state, self.budget, self.summarizer,
                       events=self.events, specs=tools)
        if rewritten is not messages:
            self.compactions += 1
        self.peak_tokens = max(
            self.peak_tokens, total_tokens(rewritten, tools)
        )
        return self.inner.complete(list(rewritten), tools)


def with_compaction(
    loop: AgentLoop,
    budget: ContextBudget,
    *,
    pinned: bool = True,
) -> AgentLoop:
    """Install the compaction middleware in front of a loop's model.

    Returns the same loop, so the call reads as a decorator at the call
    site. It has to take the loop rather than the bare provider because
    the pinned block is computed from *this run's* event log, and the loop
    is what owns that log.
    """
    loop.model = CompactingModel(
        loop.model, budget, loop.events, pinned=pinned
    )
    return loop


@dataclass
class RunOutcome:
    """One graded run, with the numbers the trade-off is argued from."""

    task: int
    mode: str
    world: World
    state: RunState | None = None
    passed: bool = False
    error: str = ""
    turns: int = 0
    compactions: int = 0
    peak_tokens: int = 0
    total_input_tokens: int = 0
    reasons: list[str] = field(default_factory=list)

    @property
    def refund_rows(self) -> int:
        """Refund rows against the damaged order. Two is the incident."""
        return len(self.world.refunds_for(ORDER))

    @property
    def refunded_cents(self) -> int:
        """What the customer was actually paid."""
        return self.world.total_refunded_cents(ORDER)


def graders() -> list[Any]:
    """Outcome first, trajectory second. Both read the world, not the run.

    ``StateGrader`` settles whether the customer was paid once or twice,
    which is the only thing that distinguishes the three configurations.
    ``TrajectoryGrader`` asserts the policy was read before the money
    moved, and caps repeats, which catches a run that reaches the right
    total by an expensive route.
    """
    return [
        StateGrader()
        .refunded(ORDER, AMOUNT)
        .no_duplicate_refunds(ORDER),
        TrajectoryGrader(
            required=["get_order", "get_policy", "issue_refund"],
            before=[("get_policy", "issue_refund")],
            max_repeats=3,
        ),
    ]


def run_one(task: int, mode: str) -> RunOutcome:
    """Run one task under one configuration and grade it.

    Args:
        task: Number of filler rounds, which is what makes a session long.
        mode: One of :data:`MODES`.

    Returns:
        A :class:`RunOutcome`. A run that raises is a failure, recorded
        with its error rather than propagated: budget exhaustion is one of
        the outcomes being measured.
    """
    if mode not in MODES:
        raise ValueError(f"unknown mode {mode!r}; expected one of {MODES}")

    world = World(clock=_fixed_clock())
    session = ScriptedSession(long_session(filler_rounds=task))
    outcome = RunOutcome(task=task, mode=mode, world=world)

    loop = AgentLoop(
        model=session,
        tools=northstar_tools(world),
        checkpointer=MemoryCheckpointer(),
        policy=default_northstar_policy(),
        principal=Principal.of("CUST-8841", "refunds:write", "orders:read"),
        max_turns=64,
        budget_cents=BUDGET_CENTS,
        tool_retries=0,
    )
    if mode != "none":
        loop = with_compaction(loop, BUDGET, pinned=(mode == "pinned"))

    try:
        state = loop.run(GOAL, run_id=f"run-ch07-{mode}-{task:02d}")
    except Exception as exc:  # noqa: BLE001 - a crash is a failed run
        outcome.error = f"{type(exc).__name__}: {exc}"
        outcome.reasons.append(outcome.error)
    else:
        outcome.state = state
        verdict = grade_all(graders(), state, world)
        outcome.passed = verdict.passed
        outcome.reasons = list(verdict.reasons)

    outcome.turns = len(session.input_tokens_per_turn)
    outcome.total_input_tokens = sum(session.input_tokens_per_turn)
    # Peak is measured where the provider sees it, which is *after* any
    # middleware. Measuring it off the final state would report zero for
    # the runs that mattered most: the ones that never reached a final
    # state because their context got too expensive.
    outcome.peak_tokens = max(session.input_tokens_per_turn, default=0)
    if isinstance(loop.model, CompactingModel):
        outcome.compactions = loop.model.compactions
    return outcome


def measure(compaction: bool, *, pinned: bool = True) -> ReliabilityReport:
    """Grade the twelve-task set under one configuration.

    Args:
        compaction: Whether the middleware runs at all.
        pinned: When it does, whether it prepends the computed facts.

    Returns:
        A :class:`ReliabilityReport` carrying pass@1, pass^k, and the
        Wilson interval. Twelve tasks is a small sample and the interval
        says so, which is the honest way to report it.
    """
    mode = "none" if not compaction else ("pinned" if pinned else "naive")
    return run_repeated(
        task=lambda seed: run_one(TASKS[seed % len(TASKS)], mode).passed,
        n=len(TASKS),
        seed=0,
        name=f"compaction={mode}",
        k_values=(1, 2, 4, 8),
    )


def compare() -> dict[str, list[RunOutcome]]:
    """Run every task under every configuration. What the demo prints."""
    return {
        mode: [run_one(task, mode) for task in TASKS] for mode in MODES
    }


def summarise(outcomes: Sequence[RunOutcome]) -> dict[str, Any]:
    """Roll one configuration's runs up into the row of a table."""
    passed = sum(1 for o in outcomes if o.passed)
    duplicates = sum(1 for o in outcomes if o.refund_rows > 1)
    return {
        "tasks": len(outcomes),
        "passed": passed,
        "duplicate_refunds": duplicates,
        "compactions": sum(o.compactions for o in outcomes),
        "peak_tokens": max((o.peak_tokens for o in outcomes), default=0),
        "tokens_per_run": (
            sum(o.total_input_tokens for o in outcomes) // len(outcomes)
            if outcomes
            else 0
        ),
        "tokens_per_turn": (
            sum(o.total_input_tokens for o in outcomes)
            // max(1, sum(o.turns for o in outcomes))
        ),
        "errors": sum(1 for o in outcomes if o.error),
    }


def budget_report(outcome: RunOutcome) -> str:
    """Per-line-item accounting for one run's final context.

    This is what ``exceeded`` returning names rather than a boolean buys:
    a run that tripped its budget says which line item did it.
    """
    if outcome.state is None:
        return "(run did not finish; nothing to account)"
    used = account(outcome.state.messages, World().tool_specs())
    over = BUDGET.exceeded(used)
    body = " ".join(f"{k}={v}" for k, v in used.items())
    return f"{body} over={over or ['none']}"


def _fixed_clock() -> Any:
    """A counter standing in for a wall clock, so runs are comparable."""
    ticks = iter(range(1, 10_000))

    def clock() -> float:
        return float(next(ticks))

    return clock
