"""Pattern four: a separate model call, with a fresh context and a rubric.

A critic pass is meaningfully better than self-reflection, because the
mistake is not sitting in its context pre-labelled as correct. It is the
pattern worth having for outputs whose quality is a judgment call: the
customer-facing message, the summary, the generated query.

Both of its own failure modes are bounded by configuration — endless
polishing, fixed by a hard iteration cap, and judge bias, fixed by an
external rubric and by not using the generator as its own judge.

What a critic cannot do is see outside what you hand it. Hand it the
agent's narration and it inherits the agent's blind spot, which is why this
build approves the double refund in the second half of the demo. Its
verdict is correct about everything it was shown.
"""

from __future__ import annotations

from dataclasses import dataclass

import task
from northstar_contracts import Message, RunState, World
from northstar_runtime import FakeModel, ModelProvider
from task import Meter, Metered, Pattern

__all__ = ["MAX_REVISIONS", "RUBRIC", "Critique", "build_critic", "critique"]

#: The external rubric. It is written down, it is not the generator's own
#: opinion of good, and it says nothing about the world — it cannot, because
#: the critic is not shown the world.
RUBRIC = (
    "1. States what actually happened.\n"
    "2. Names the amount in currency the customer will recognise.\n"
    "3. Promises nothing outside Northstar's control.\n"
    "4. Is under three sentences.\n"
)

#: A hard iteration cap. Without one, "polish this" is a loop with no exit.
MAX_REVISIONS = 1


@dataclass(frozen=True)
class Critique:
    """One review verdict, from a context that saw only the message."""

    approved: bool
    verdict: str

    @property
    def summary(self) -> str:
        """A one-line rendering for the demo output."""
        return self.verdict.replace("\n", " ")


def critique(model: ModelProvider, message: str) -> Critique:
    """Review one outbound message against the rubric. Fresh context.

    The prompt carries the rubric and the candidate and nothing else. No
    transcript, no tool results, no plan: a critic that can see the run's
    reasoning is a critic that can be talked round by it.
    """
    prompt = (
        "You are reviewing a customer support message against a rubric.\n"
        f"Rubric:\n{RUBRIC}\nMessage:\n{message}\n\n"
        "Answer APPROVE or REVISE on the first line, then one sentence."
    )
    reply = model.complete([Message(role="user", content=prompt)], tools=[])
    verdict = str(reply.text or "").strip()
    return Critique(
        approved=verdict.upper().startswith("APPROVE"), verdict=verdict
    )


#: The scripted verdict. It approves, and it is right to: the message is
#: accurate about everything it mentions. What it does not mention is that
#: the money moved twice, and nothing in a transcript would have told it.
CRITIC_SCRIPT = [
    "APPROVE: states the outcome, names the amount, promises nothing extra."
]


def build_critic(world: World) -> Pattern:
    """The baseline loop, plus one review pass over the outbound message."""
    meter = Meter()
    loop = task.build_loop(world, meter)
    reviewer = Metered(FakeModel(default=CRITIC_SCRIPT), meter)
    ref: dict[str, Pattern] = {}

    def run(goal: str) -> RunState:
        result = loop.run(goal, run_id="run_ch04_critic")
        verdict = critique(reviewer, result.final_text or "")
        me = ref["pattern"]
        me.notes.append(verdict.summary)
        # A critic can only flag what it was shown, and it was shown the
        # message. ``caught`` stays False even when the ledger is wrong.
        me.caught = False
        return result

    pattern = Pattern(
        name="Critic pass on the message", meter=meter, runner=run
    )
    ref["pattern"] = pattern
    return pattern
