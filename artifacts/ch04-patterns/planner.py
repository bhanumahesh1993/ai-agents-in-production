"""Pattern three: emit a plan, check it mechanically, then execute it.

What planning buys is a reviewable object that exists before anything
happens: something you can validate against tool schemas, cap at a step
count, price, and show to a human. What it costs is at least one extra
model call, and a plan that then rides along in context for every
subsequent turn.

``validate`` is the part most implementations skip, and it is the part that
pays. ``ToolSpec.writes`` is what makes it checkable, which is one of
several reasons Chapter 11 insists every tool declares whether it mutates
the world.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

import task
from northstar_contracts import Message, RunState, ToolSpec, World
from northstar_runtime import (
    DEFAULT_SYSTEM_PROMPT,
    FakeModel,
    ModelProvider,
)
from task import Meter, Metered, Pattern

__all__ = [
    "PLAN_CAP",
    "PlanStep",
    "InvalidPlan",
    "build_planner",
    "parse_plan",
    "plan_for",
    "render",
    "validate",
]

#: Step ceiling. A plan longer than this is not a plan, it is a transcript.
PLAN_CAP = 6


@dataclass(frozen=True)
class PlanStep:
    """One proposed action, before anything has run."""

    tool: str
    why: str
    arguments: dict[str, Any]


class InvalidPlan(RuntimeError):
    """The plan failed validation, so none of it ran."""


def validate(
    plan: list[PlanStep], specs: dict[str, ToolSpec], cap: int
) -> list[str]:
    """Reject a plan before any of it runs. Returns the reasons."""
    problems: list[str] = []
    if len(plan) > cap:
        problems.append(f"{len(plan)} steps exceeds the cap of {cap}")
    reads_seen = 0
    for i, step in enumerate(plan):
        spec = specs.get(step.tool)
        if spec is None:
            problems.append(f"step {i}: unknown tool {step.tool}")
            continue
        if spec.writes and reads_seen == 0:
            problems.append(f"step {i}: writes before any read")
        if not spec.writes:
            reads_seen += 1
    return problems


#: The plan the scripted planner emits. Four steps for a four-tool task,
#: which is the over-planning anti-pattern printed as JSON: correctly
#: ordered, well structured, and completely redundant with what the loop
#: would have done unprompted.
PLAN_JSON = json.dumps(
    [
        {
            "tool": "get_order",
            "why": "confirm the line item and the refundable balance",
            "arguments": {"order_id": task.ORDER_ID},
        },
        {
            "tool": "get_policy",
            "why": "confirm damaged goods are refundable at 100 percent",
            "arguments": {"reason": task.REASON, "sku": task.SKU},
        },
        {
            "tool": "issue_refund",
            "why": "the claim is below the approval threshold",
            "arguments": {
                "order_id": task.ORDER_ID,
                "amount_cents": task.AMOUNT_CENTS,
                "reason": task.REASON,
            },
        },
        {
            "tool": "send_message",
            "why": "the customer is owed an answer",
            "arguments": {
                "order_id": task.ORDER_ID,
                "body": task.MESSAGE_BODY,
            },
        },
    ]
)

PLAN_PROMPT = (
    "Produce a plan for this Northstar ticket as a JSON array of objects "
    "with keys tool, why, arguments. Emit JSON only.\n\n"
)


def parse_plan(raw: str) -> list[PlanStep]:
    """Turn the model's JSON into typed steps.

    A plan you cannot parse is a plan you cannot check, so a parse failure
    is a validation failure and not something to paper over.
    """
    try:
        rows = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise InvalidPlan(f"plan was not JSON: {exc}") from exc
    if not isinstance(rows, list):
        raise InvalidPlan("plan must be a JSON array of steps")
    return [
        PlanStep(
            tool=str(row.get("tool", "")),
            why=str(row.get("why", "")),
            arguments=dict(row.get("arguments") or {}),
        )
        for row in rows
    ]


def plan_for(model: ModelProvider, goal: str) -> list[PlanStep]:
    """Ask for a plan. One model call, and not a cheap one."""
    reply = model.complete(
        [Message(role="user", content=PLAN_PROMPT + goal)], tools=[]
    )
    return parse_plan(str(reply.text))


def render(plan: list[PlanStep]) -> str:
    """The plan as prompt text, which is what makes it cost tokens."""
    lines = [f"{i + 1}. {s.tool}: {s.why}" for i, s in enumerate(plan)]
    return "Approved plan:\n" + "\n".join(lines)


def build_planner(world: World) -> Pattern:
    """Plan, validate, then execute with the plan in context."""
    meter = Meter()
    planner_model = Metered(FakeModel(default=[PLAN_JSON]), meter)
    specs = {s.name: s for s in world.tool_specs()}

    def run(goal: str) -> RunState:
        plan = plan_for(planner_model, goal)
        problems = validate(plan, specs, PLAN_CAP)
        if problems:
            raise InvalidPlan("; ".join(problems))
        # The plan is appended to the standing prompt rather than replacing
        # it, which is what makes planning cost tokens: the plan rides along
        # in context on every subsequent turn, on top of everything the
        # baseline was already sending.
        loop = task.build_loop(
            world,
            meter,
            system_prompt=(
                DEFAULT_SYSTEM_PROMPT
                + "\n\nExecute the plan below, revalidating each mutating "
                "step against current state immediately before you dispatch "
                "it. Abandon the plan if the world no longer matches it.\n\n"
                + render(plan)
            ),
        )
        return loop.run(goal, run_id="run_ch04_planner")

    return Pattern(name="Plan-and-execute", meter=meter, runner=run)
