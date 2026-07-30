"""Run one benchmark attempt: fresh world, scripted user, graded state.

:func:`attempt` is the whole adapter. Everything a public leaderboard reports
comes from calling it once per task and averaging. Everything you need to
make a release decision comes from calling it ``k`` times per task and looking
at the distribution, which is what ``report.py`` does.

Three mechanisms live here and each one exists because a benchmark that
leaves it out measures something easier than the work.

**Fresh fixtures per attempt.** ``world_from_fixtures`` rebuilds the world and
removes every order the task did not declare, so an attempt cannot pass by
touching a record it was never given, and attempt 2 cannot inherit attempt 1's
refund.

**Dual control.** A task with ``user_actions`` does not complete unless the
customer performs those steps, and the customer performs them only if the
agent asks in terms they recognise. That is the τ²-bench setting reduced to
its mechanism: acting requires modelling one world, guiding requires modelling
a world plus another actor's understanding of it.

**Forbidden actions are graded separately from the outcome.** A task where the
agent refunded 24,000 cents on a fraud-flagged order is a failure even if the
customer ends the conversation satisfied.
"""

from __future__ import annotations

import random
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from northstar_contracts import (
    Money,
    ToolCall,
    ToolSpec,
    ToolValidationError,
    World,
    short_hash,
)
from northstar_evals import (
    GradeResult,
    Persona,
    SimulatedUser,
    StateGrader,
    TrajectoryGrader,
    Turn,
    grade_all,
)
from northstar_runtime import (
    AgentLoop,
    FakeModel,
    FlakyModel,
    ModelProvider,
    ToolRegistry,
)
from task import BenchmarkTask

__all__ = [
    "ACTION_CUES",
    "COMPLIANCE",
    "AttemptResult",
    "DualControl",
    "attempt",
    "flaky_model",
    "grader_for",
    "northstar_registry",
    "plan_for",
    "world_from_fixtures",
]

#: What the agent has to say for the customer to recognise the request. A
#: dual-control step the agent never asks for clearly is a step that never
#: happens, which is the communication failure an outcome-only benchmark
#: records as a policy failure.
ACTION_CUES: dict[str, str] = {
    "photo_of_damage": "photo",
    "confirm_address": "delivery address",
    "confirm_card_last4": "last four digits",
    "check_second_box": "second box",
    "return_label_used": "return label",
}

#: How often a customer who was asked correctly actually does the thing.
#: A declared parameter of the simulator, not a measurement. It is set near
#: the magnitude τ²-bench reports for the dual-control drop, so the gap the
#: report prints between solo and guided work is the right order of size.
COMPLIANCE = 0.82

#: The flakiness profile every attempt runs under. Low, so that the visible
#: gap in the report comes from dual control rather than from the wrapper.
P_REPEAT = 0.002
P_STALL = 0.006
P_GIVEUP = 0.0015

WRITE_TOOLS = frozenset(
    {"issue_refund", "escalate_to_specialist"}
)


@dataclass(frozen=True)
class AttemptResult(GradeResult):
    """A :class:`GradeResult` that also carries what the attempt cost.

    A benchmark report needs the cost, the turn count and the latency of
    the same run it graded, and a bare ``GradeResult`` has nowhere to put
    them. Subclassing keeps ``attempt`` returning a ``GradeResult``, which
    is what the chapter's excerpt promises, while ``report.py`` can still
    read ``a.cost_cents`` off an attempt.
    """

    task_id: str = ""
    seed: int = 0
    cost_cents: Money = 0
    turns: int = 0
    latency_ms: float = 0.0
    dual_control: bool = False


class DualControl:
    """The steps only the customer can perform, and whether they did.

    Args:
        actions: Action ids from :data:`ACTION_CUES`.
        seed: Fixed per attempt, so a refusal is reproducible.
        task_id: Folded into the seed so two tasks asking for the same
            action on the same run do not get the same answer.

    Raises:
        KeyError: On an action with no declared cue. A dual-control step
            the harness cannot phrase is a step no agent could ever get
            right, and failing loudly beats scoring it zero forever.
    """

    def __init__(
        self,
        actions: list[str],
        *,
        seed: int = 0,
        task_id: str = "",
    ) -> None:
        for action in actions:
            if action not in ACTION_CUES:
                known = ", ".join(sorted(ACTION_CUES))
                raise KeyError(
                    f"no cue for user action {action!r}; known: {known}"
                )
        self.actions = list(actions)
        self.seed = seed
        self.task_id = task_id
        self.performed: set[str] = set()
        self.asked: set[str] = set()

    @property
    def satisfied(self) -> bool:
        """Whether every required step has actually happened."""
        return all(a in self.performed for a in self.actions)

    @property
    def outstanding(self) -> list[str]:
        """Steps still waiting on the customer."""
        return [a for a in self.actions if a not in self.performed]

    def request(self, body: str) -> str | None:
        """Read an outbound message and let the customer act on it.

        Returns:
            What the customer says back about the request, or ``None`` if
            the message did not ask for anything outstanding.
        """
        lowered = body.lower()
        replies: list[str] = []
        for action in self.outstanding:
            if ACTION_CUES[action] not in lowered:
                continue
            self.asked.add(action)
            rng = random.Random(f"{self.seed}:{self.task_id}:{action}")
            if rng.random() < COMPLIANCE:
                self.performed.add(action)
                replies.append(f"Done: {action.replace('_', ' ')}.")
            else:
                replies.append(
                    f"I cannot do that right now ({action.replace('_', ' ')})."
                )
        return " ".join(replies) if replies else None


def world_from_fixtures(order_ids: list[str]) -> World:
    """A fresh world holding only the declared orders.

    Chapter 14's excerpt writes this as ``World.from_fixtures(...)``. It is
    a function here rather than a classmethod because ``World`` belongs to
    ``northstar_contracts`` and an artifact does not get to grow the
    contracts package a method that only one chapter needs.

    Raises:
        KeyError: If a task names a fixture the world does not ship.
    """
    world = World()
    wanted = set(order_ids)
    missing = wanted - set(world.orders)
    if missing:
        raise KeyError(
            f"no such fixture order(s): {', '.join(sorted(missing))}"
        )
    for order_id in list(world.orders):
        if order_id not in wanted:
            del world.orders[order_id]
    return world


def northstar_registry(
    world: World,
    user: SimulatedUser,
    control: DualControl,
) -> ToolRegistry:
    """The Northstar support scope, with the user wired into it.

    ``send_message`` returns the customer's reply, which is what lets a
    multi-turn task progress at all, and it is also where a dual-control
    request reaches the person who has to act on it. The write tools refuse
    while anything is outstanding, because the specialist queue and the
    refund service both require the evidence the customer holds.
    """
    registry = ToolRegistry(inject_idempotency_key=True)
    for spec, fn in world.tools():
        registry.register(spec, _wrap(spec, fn, user, control))
    return registry


def _wrap(
    spec: ToolSpec,
    fn: Callable[..., Any],
    user: SimulatedUser,
    control: DualControl,
) -> Callable[..., Any]:
    """Attach the simulated user and the dual-control gate to one tool."""
    if spec.name == "send_message":

        def send_message(**kwargs: Any) -> dict[str, Any]:
            record = dict(fn(**kwargs))
            body = str(kwargs.get("body", ""))
            reply = control.request(body)
            if reply is None:
                reply = user.reply(body)
            record["customer_reply"] = reply
            return record

        return send_message

    if spec.name in WRITE_TOOLS:

        def gated(**kwargs: Any) -> Any:
            if not control.satisfied:
                waiting = ", ".join(control.outstanding)
                raise ToolValidationError(
                    f"{spec.name} needs the customer to complete: {waiting}. "
                    "Ask for it in a message first."
                )
            return fn(**kwargs)

        return gated

    return fn


def plan_for(task: BenchmarkTask) -> list[Any]:
    """The reference trajectory for one task.

    One planner for all forty tasks rather than forty hand-written scripts.
    That is what makes the set a benchmark: every task faces the same agent,
    so a difference between two tasks is a property of the tasks.
    """
    order = task.primary_order
    steps: list[Any] = [
        ToolCall("c1", "get_order", {"order_id": order}),
    ]
    if task.reason:
        arguments: dict[str, Any] = {"reason": task.reason}
        if task.sku:
            arguments["sku"] = task.sku
        steps.append(ToolCall("c2", "get_policy", arguments))
    if task.user_actions:
        cues = ", ".join(ACTION_CUES[a] for a in task.user_actions)
        steps.append(
            ToolCall(
                "c3",
                "send_message",
                {
                    "order_id": order,
                    "body": (
                        "Before I can finish this I need one thing from you: "
                        f"{cues}. Could you send that over?"
                    ),
                },
            )
        )
    if task.expected_refund_cents:
        steps.append(
            ToolCall(
                "c4",
                "issue_refund",
                {
                    "order_id": order,
                    "amount_cents": task.expected_refund_cents,
                    "reason": task.reason or "damaged",
                },
            )
        )
    if task.expect_escalation:
        steps.append(
            ToolCall(
                "c5",
                "escalate_to_specialist",
                {"order_id": order, "reason": task.reason or "review"},
            )
        )
    steps.append(
        ToolCall(
            "c6",
            "send_message",
            {"order_id": order, "body": _closing_body(task)},
        )
    )
    steps.append(_closing_body(task))
    return steps


def _closing_body(task: BenchmarkTask) -> str:
    """What the agent tells the customer at the end."""
    if task.expected_refund_cents:
        return f"Refunded {task.expected_refund_cents} cents to your card."
    if task.expect_escalation:
        return "A specialist is picking this up and will be in touch."
    return "Here is the current status of your order."


def grader_for(task: BenchmarkTask) -> list[Any]:
    """The graders for one task: authoritative state, then forbidden paths.

    ``expected_refund_cents`` is asserted against the refund ledger and
    never against the assistant's final message, which is the part of the
    system least connected to whether money moved.
    """
    order = task.primary_order
    state = (
        StateGrader()
        .refunded(order, task.expected_refund_cents)
        .no_duplicate_refunds(order)
    )
    if task.expect_escalation:
        state = state.escalated(order)
    graders: list[Any] = [state]
    if task.forbidden_tools:
        graders.append(
            TrajectoryGrader(forbidden=sorted(task.forbidden_tools))
        )
    return graders


def attempt_seed(task: BenchmarkTask, seed: int) -> int:
    """Mix the task id into the attempt seed.

    Without this, attempt 3 of every task of the same length draws the
    same interference from the same run seed, and forty tasks report one
    number in forty rows. Hashing the pair decorrelates the tasks and
    keeps the whole set reproducible from the single seed in configuration.
    """
    return int(short_hash(f"{task.task_id}:{seed}", 8), 16)


def flaky_model(task: BenchmarkTask, seed: int) -> ModelProvider:
    """The seeded provider one attempt runs against."""
    base = FakeModel(default=plan_for(task), strict=False)
    return FlakyModel(
        base,
        seed=attempt_seed(task, seed),
        p_repeat=P_REPEAT,
        p_stall=P_STALL,
        p_giveup=P_GIVEUP,
    )


def attempt(
    task: BenchmarkTask,
    model: ModelProvider | None,
    seed: int,
) -> GradeResult:
    """Run ``task`` once and grade the world it left behind.

    Args:
        task: The task to attempt.
        model: The provider to run against. ``None`` builds the seeded
            flaky model this benchmark ships with, which is what makes the
            whole thing reproducible offline.
        seed: Fixes the model's draws and the customer's compliance.

    Returns:
        An :class:`AttemptResult`, which is a ``GradeResult`` carrying the
        attempt's cost, turn count, and latency.
    """
    started = time.perf_counter()
    world = world_from_fixtures(task.initial_orders)   # fresh per attempt
    user = SimulatedUser(
        Persona(
            name=task.task_id,
            opening=task.goal,
            turns=tuple(Turn(line) for line in task.user_script),
        )
    )
    control = DualControl(
        task.user_actions, seed=seed, task_id=task.task_id
    )
    loop = AgentLoop(
        model or flaky_model(task, seed),
        northstar_registry(world, user, control),
        max_turns=task.max_turns,
        budget_cents=task.budget_cents,
    )

    reasons: list[str] = []
    try:
        state = loop.run(task.goal, run_id=f"{task.task_id}-{seed}")
    except Exception as exc:  # noqa: BLE001 - a dead run is a failed task
        elapsed = (time.perf_counter() - started) * 1000.0
        return AttemptResult(
            passed=False,
            score=0.0,
            reasons=[f"{type(exc).__name__}: {exc}"],
            grader="attempt",
            details={"world": world.snapshot()},
            task_id=task.task_id,
            seed=seed,
            cost_cents=task.budget_cents,
            turns=task.max_turns,
            latency_ms=elapsed,
            dual_control=task.is_dual_control,
        )

    result = grade_all(grader_for(task), state, world)
    reasons = list(result.reasons)
    if task.is_dual_control and not control.satisfied:
        reasons.append(
            "dual control: the customer never completed "
            + ", ".join(control.outstanding)
        )
    elapsed = (time.perf_counter() - started) * 1000.0
    return AttemptResult(
        passed=result.passed,
        score=result.score,
        reasons=reasons,
        grader=result.grader,
        details={
            **result.details,
            # Hoisted to the top level so a report or a test can read the
            # ledger without unpacking one grader's private details.
            "world": world.snapshot(),
            "asked_for": sorted(control.asked),
            "performed": sorted(control.performed),
        },
        task_id=task.task_id,
        seed=seed,
        cost_cents=state.budget_spent_cents,
        turns=state.step,
        latency_ms=elapsed,
        dual_control=task.is_dual_control,
    )
