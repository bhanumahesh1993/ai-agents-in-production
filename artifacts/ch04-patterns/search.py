"""Pattern six: best-of-three over the read-only part, then one write.

Branching costs multiply rather than add: branching factor times depth, with
every surviving branch carrying its own accumulated context. And you cannot
branch a side effect — a search over a space containing ``issue_refund`` is
n refunds.

So this build searches where searching is free. Three candidate plans are
generated and scored against the retrieved policy, one is selected, and only
then does a single execution loop run. That is the affordable production
form of the pattern: most of the quality gain, cost linear in one small
step, and the write path still a single auditable action.

Note what the search does *not* fix. Sampling one model n times gives you n
samples from one distribution, so a systematic misreading appears in every
branch. The winning plan here omits an idempotency key, and so do the two
that lost.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

import planner
import task
from northstar_contracts import Message, RunState, World
from northstar_runtime import (
    DEFAULT_SYSTEM_PROMPT,
    FakeModel,
    ModelProvider,
)
from task import Meter, Metered, Pattern

__all__ = ["BRANCHES", "Candidate", "build_search", "score", "search_plans"]

#: Branching factor. Three is the number the chapter measures; the cost is
#: linear in it and so is the rate-limit pressure.
BRANCHES = 3


@dataclass(frozen=True)
class Candidate:
    """One generated plan and the score an external pass gave it."""

    index: int
    steps: list[planner.PlanStep]
    score: float
    reason: str


#: Three candidate plans, keyed by the sample tag the generator was asked
#: for. They differ in the reads they do first and in whether they message
#: the customer before or after the refund. None of them carries an
#: idempotency key, because they all came from one distribution, which is
#: the point of the paragraph above.
_PLAN_0 = json.dumps(
    [
        {"tool": "get_order", "why": "confirm the line item",
         "arguments": {"order_id": task.ORDER_ID}},
        {"tool": "get_policy", "why": "confirm eligibility",
         "arguments": {"reason": task.REASON, "sku": task.SKU}},
        {"tool": "issue_refund", "why": "below the threshold",
         "arguments": {"order_id": task.ORDER_ID,
                       "amount_cents": task.AMOUNT_CENTS,
                       "reason": task.REASON}},
        {"tool": "send_message", "why": "close the loop",
         "arguments": {"order_id": task.ORDER_ID,
                       "body": task.MESSAGE_BODY}},
    ]
)

_PLAN_1 = json.dumps(
    [
        {"tool": "get_order", "why": "confirm the line item",
         "arguments": {"order_id": task.ORDER_ID}},
        {"tool": "send_message", "why": "reassure the customer first",
         "arguments": {"order_id": task.ORDER_ID,
                       "body": task.MESSAGE_BODY}},
        {"tool": "issue_refund", "why": "then pay",
         "arguments": {"order_id": task.ORDER_ID,
                       "amount_cents": task.AMOUNT_CENTS,
                       "reason": task.REASON}},
    ]
)

_PLAN_2 = json.dumps(
    [
        {"tool": "search_orders", "why": "look for related tickets",
         "arguments": {"customer_id": "CUST-8841"}},
        {"tool": "get_order", "why": "confirm the line item",
         "arguments": {"order_id": task.ORDER_ID}},
        {"tool": "get_policy", "why": "confirm eligibility",
         "arguments": {"reason": task.REASON, "sku": task.SKU}},
        {"tool": "issue_refund", "why": "below the threshold",
         "arguments": {"order_id": task.ORDER_ID,
                       "amount_cents": task.AMOUNT_CENTS,
                       "reason": task.REASON}},
        {"tool": "send_message", "why": "close the loop",
         "arguments": {"order_id": task.ORDER_ID,
                       "body": task.MESSAGE_BODY}},
    ]
)

CANDIDATE_PLANS: dict[str, list[str]] = {
    "sample 0": [_PLAN_0],
    "sample 1": [_PLAN_1],
    "sample 2": [_PLAN_2],
}

#: One scoring reply per candidate, from a scorer with its own context. The
#: second candidate promises a replacement before the refund has landed and
#: is scored down for it; the third pays for a search the ticket did not
#: need. Neither the scorer nor any candidate notices the missing key.
SCORER_REPLIES: dict[str, list[str]] = {
    "candidate 0": ["0.9 reads the order and the policy before it pays"],
    "candidate 1": ["0.3 promises the customer before the refund lands"],
    "candidate 2": ["0.6 correct, but searches for no reason"],
}

SCORE_PROMPT = (
    "Score this Northstar plan from 0 to 1 against the shipped refund "
    "policy. Reply with the number, then one clause of justification.\n\n"
)

#: Each sample is tagged, so the scripted generator can return a different
#: draft per branch and the scripted scorer can answer per candidate. A real
#: model would vary by temperature; a deterministic one has to be asked
#: three visibly different questions.
SAMPLE_TAG = "sample {i}"
CANDIDATE_TAG = "candidate {i}"


def score(
    model: ModelProvider, raw_plan: str, label: str, policy: str = ""
) -> tuple[float, str]:
    """Score one candidate against the retrieved policy. One call per branch.

    The policy text travels with every scoring call, which is where the
    multiplicative cost of branching actually shows up: the evidence is not
    shared between branches, it is re-sent with each of them.
    """
    reply = model.complete(
        [
            Message(
                role="user",
                content=(
                    f"{SCORE_PROMPT}({label})\n{raw_plan}\n\n"
                    f"Shipped policy:\n{policy}"
                ),
            )
        ],
        tools=[],
    )
    text = str(reply.text or "0").strip()
    head, _, reason = text.partition(" ")
    try:
        value = float(head)
    except ValueError:
        # An unparseable score is a zero, not a free pass. Fail closed.
        return 0.0, f"unscoreable: {text}"
    return value, reason


def search_plans(
    generator: ModelProvider,
    scorer: ModelProvider,
    goal: str,
    policy: str = "",
    branches: int = BRANCHES,
) -> list[Candidate]:
    """Generate ``branches`` plans and score each one. All read-only."""
    candidates: list[Candidate] = []
    for i in range(branches):
        tag = SAMPLE_TAG.format(i=i)
        reply = generator.complete(
            [
                Message(
                    role="user",
                    content=(
                        f"{planner.PLAN_PROMPT}({tag})\n{goal}\n\n"
                        f"Shipped policy:\n{policy}"
                    ),
                )
            ],
            tools=[],
        )
        raw = str(reply.text or "[]")
        value, reason = score(
            scorer, raw, CANDIDATE_TAG.format(i=i), policy
        )
        candidates.append(
            Candidate(
                index=i,
                steps=planner.parse_plan(raw),
                score=value,
                reason=reason,
            )
        )
    return candidates


def build_search(world: World) -> Pattern:
    """Search the plans, execute the winner once."""
    meter = Meter()
    generator = Metered(FakeModel(CANDIDATE_PLANS), meter)
    scorer = Metered(FakeModel(SCORER_REPLIES), meter)
    specs = {s.name: s for s in world.tool_specs()}
    ref: dict[str, Pattern] = {}

    def run(goal: str) -> RunState:
        # The search is over a read-only sub-problem, so the evidence it
        # scores against is fetched once, with a read, and no model call.
        policy = json.dumps(
            world.get_policy(reason=task.REASON, sku=task.SKU), indent=1
        )
        candidates = search_plans(generator, scorer, goal, policy)
        # Validation still applies to the winner. A search that skips it has
        # bought three chances to produce an unexecutable plan.
        viable = [
            c
            for c in candidates
            if not planner.validate(c.steps, specs, planner.PLAN_CAP)
        ]
        if not viable:
            raise planner.InvalidPlan("every candidate failed validation")
        best = max(viable, key=lambda c: c.score)
        me = ref["pattern"]
        me.notes.append(
            f"{len(candidates)} plans generated, {len(viable)} viable, "
            f"picked #{best.index} at {best.score:.2f}"
        )
        loop = task.build_loop(
            world,
            meter,
            system_prompt=(
                DEFAULT_SYSTEM_PROMPT
                + "\n\nExecute the selected plan, revalidating each mutating "
                "step against current state before dispatch.\n\n"
                + planner.render(best.steps)
            ),
        )
        return loop.run(goal, run_id="run_ch04_search")

    pattern = Pattern(name="Best-of-3 plan search", meter=meter, runner=run)
    ref["pattern"] = pattern
    return pattern
