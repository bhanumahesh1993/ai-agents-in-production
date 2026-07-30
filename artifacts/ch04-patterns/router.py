"""Pattern two: the model chooses once, then code takes the decision back.

A router asks the model for exactly one thing — a destination label —
validates it against a fixed set, and hands everything after that to code.
Two lines carry the weight: the label is checked against a closed set, so a
hallucinated destination cannot reach a dispatcher, and the fallback is the
safest branch rather than the most common one.

The cost is usually negative, which surprises people. The refund branch
carries four tool specifications instead of six and a system prompt written
for one job, and tool definitions are part of the prompt on every turn.
"""

from __future__ import annotations

import task
from northstar_contracts import Message, RunState, World
from northstar_runtime import FakeModel, ModelProvider
from task import Meter, Metered, Pattern

__all__ = ["ROUTES", "ROUTE_PROMPT", "SYSTEM_PROMPTS", "build_router", "route"]

ROUTES: dict[str, list[str]] = {
    "refund": ["get_order", "get_policy", "issue_refund",
               "send_message"],
    "status": ["get_order", "send_message"],
    "fraud": ["escalate_to_specialist"],
}

#: One short prompt per branch, instead of one prompt that has to cover
#: every case. This is half of what routing actually buys.
SYSTEM_PROMPTS: dict[str, str] = {
    "refund": (
        "You handle Northstar refund requests. Read the order, read the "
        "refund policy for the SKU and reason, issue one refund in integer "
        "cents, then tell the customer in one sentence."
    ),
    "status": (
        "You answer Northstar order-status questions. Read the order and "
        "reply. You cannot move money."
    ),
    "fraud": (
        "You triage suspected fraud. Escalate to the specialist queue and "
        "decide nothing yourself."
    ),
}

ROUTE_PROMPT = (
    "Classify this Northstar support ticket into exactly one of: refund, "
    "status, fraud. Reply with the single word and nothing else.\n\n"
)


def route(model: ModelProvider, goal: str) -> str:
    """The model picks one label. Code owns everything after it."""
    reply = model.complete(
        [Message(role="user", content=ROUTE_PROMPT + goal)],
        tools=[],
    )
    label = str(reply.text).strip().lower()
    # An unrecognised label is a routing failure, not a free choice.
    return label if label in ROUTES else "fraud"


def build_router(world: World) -> Pattern:
    """Classify once, then run a loop that only holds one branch's tools."""
    meter = Meter()
    classifier = Metered(FakeModel(default=["refund"]), meter)

    def run(goal: str) -> RunState:
        label = route(classifier, goal)
        loop = task.build_loop(
            world,
            meter,
            tool_names=ROUTES[label],
            system_prompt=SYSTEM_PROMPTS[label],
        )
        return loop.run(goal, run_id=f"run_ch04_router_{label}")

    return Pattern(name="Router plus specialist", meter=meter, runner=run)
