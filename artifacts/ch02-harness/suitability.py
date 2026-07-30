"""The suitability gate Chapter 1 promised, as a checklist that fails closed.

An agent is justified when all six of these hold. Not most of them: all of
them. The gate returns a verdict rather than a score, because "four out of
six" is not a decision anyone can act on, and because the cheap failure mode
of a gate like this is a team talking itself up to a passing average.

Fails closed in two senses. An unanswered condition counts as unmet, so a
worksheet somebody skipped does not read as approval. And any unmet condition
carries the alternative it points at, because the useful output of this gate
is usually "build a workflow", not "do nothing".

    >>> verdict = assess(REFUND_TRIAGE)
    >>> verdict.build_an_agent
    True
    >>> assess(ADDRESS_CHANGE).build_an_agent
    False
"""

from __future__ import annotations

from dataclasses import dataclass, field

__all__ = [
    "ADDRESS_CHANGE",
    "CONDITIONS",
    "REFUND_TRIAGE",
    "Condition",
    "Verdict",
    "assess",
]


@dataclass(frozen=True)
class Condition:
    """One of the six conditions, with what to do when it fails.

    Args:
        key: Stable identifier, used as the answer key. Renaming it breaks
            every filled-in worksheet, so treat it as an interface.
        question: The condition, as a question a reviewer can answer.
        instead: What to build when the answer is no.
    """

    key: str
    question: str
    instead: str


CONDITIONS: tuple[Condition, ...] = (
    Condition(
        "ambiguous",
        "Does the task contain genuine ambiguity or changing paths that "
        "deterministic code handles poorly?",
        "a workflow, or a form; a model adds variance and cost and nothing "
        "else to a task whose steps are known",
    ),
    Condition(
        "verifiable",
        "Can success be verified from state, evidence, or a clear rubric?",
        "nothing yet; without an oracle you cannot tell a good run from a "
        "bad one, so you cannot improve or defend the system",
    ),
    Condition(
        "bounded_tools",
        "Does the environment expose tools with bounded, testable "
        "contracts?",
        "the tools first; an agent over an unbounded API is an agent whose "
        "blast radius you cannot state",
    ),
    Condition(
        "containable",
        "Can wrong actions be prevented, detected, contained, or reversed?",
        "a human-in-the-loop workflow; irreversible actions with no "
        "compensation belong behind an approval, not behind a model",
    ),
    Condition(
        "org_capacity",
        "Can your organisation carry the identity, telemetry, evaluation, "
        "and incident burden autonomy creates?",
        "a smaller system, or the same system on an internal task, until "
        "the burden is one you have actually carried",
    ),
    Condition(
        "measured_gain",
        "Does the measured improvement over a simpler workflow justify the "
        "added latency, cost, and risk?",
        "the simpler workflow, and an evaluation that would change your "
        "mind",
    ),
)


@dataclass(frozen=True)
class Verdict:
    """The gate's answer, and the reasoning behind it."""

    build_an_agent: bool
    unmet: list[Condition] = field(default_factory=list)
    unanswered: list[str] = field(default_factory=list)

    def report(self) -> list[str]:
        """Lines to print: the failures, each with its alternative."""
        if self.build_an_agent:
            return ["all six conditions met: an agent is warranted"]
        lines = []
        for condition in self.unmet:
            answer = (
                "unanswered"
                if condition.key in self.unanswered
                else "answered no"
            )
            lines.append(f"{condition.key}: {answer} -> build {condition.instead}")
        return lines


def assess(answers: dict[str, bool]) -> Verdict:
    """Run the gate over a filled-in worksheet.

    Args:
        answers: Condition key to ``True`` or ``False``. A key that is
            missing is treated as ``False``, which is the fail-closed half
            of the design.

    Returns:
        A :class:`Verdict`. ``build_an_agent`` is ``True`` only when all six
        conditions were answered and answered yes.
    """
    unanswered = [c.key for c in CONDITIONS if c.key not in answers]
    unmet = [c for c in CONDITIONS if not answers.get(c.key, False)]
    return Verdict(
        build_an_agent=not unmet,
        unmet=unmet,
        unanswered=unanswered,
    )


#: Damaged-item triage. The customer's description is unstructured, the
#: relevant policy depends on the SKU and the reason, the number of lookups
#: is not knowable in advance, and the outcome is verifiable against the
#: refund ledger.
REFUND_TRIAGE: dict[str, bool] = {
    "ambiguous": True,
    "verifiable": True,
    "bounded_tools": True,
    "containable": True,
    "org_capacity": True,
    "measured_gain": True,
}

#: An address change on an unshipped order. Same channel, same customer,
#: same team, and not an agent: the rules are known, the eligibility check
#: is a database lookup, and a model adds variance and cost. A team that
#: builds one agent for "support" pays agent-grade latency and risk on the
#: half that needed a form.
ADDRESS_CHANGE: dict[str, bool] = {
    "ambiguous": False,
    "verifiable": True,
    "bounded_tools": True,
    "containable": True,
    "org_capacity": True,
    "measured_gain": False,
}
