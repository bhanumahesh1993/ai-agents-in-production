"""MAST: fourteen failure modes in three categories, plus the local ones.

MAST, the Multi-Agent System Failure Taxonomy, was built by Cemri et al. from
roughly 150 traces averaging more than 15,000 lines each, coded with grounded
theory, validated at a Cohen's kappa of 0.88, and applied to a 1,642-trace
corpus. It is the closest thing the field has to a shared defect vocabulary,
and its central conclusion is load-bearing: improvements in base model
capability alone will be insufficient to address the full taxonomy, because
well over three-quarters of observed failures were introduced by the
specification and the coordination *around* the model.

The prevalences below are properties of that corpus and not of your system.
They tell you which modes are common enough in multi-agent systems generally
to be worth building detectors for first. They do not tell you your
distribution, and a team that plans against 41.8% without labelling a single
one of its own traces has substituted someone else's measurement for its own.

Local modes are namespaced under ``LOCAL-`` so that counts stay comparable to
the published ones and a new engineer can see at a glance which vocabulary
they are reading. The rule for adding one: the failure recurs, its mechanism
fits in a sentence, and it would be fixed differently from every existing mode.
"""

from __future__ import annotations

from dataclasses import dataclass

__all__ = [
    "CATEGORIES",
    "LOCAL_MODES",
    "MAST_MODES",
    "MODES",
    "Category",
    "Mode",
    "category_of",
    "describe",
    "is_local",
    "modes_in",
]


@dataclass(frozen=True)
class Category:
    """One MAST category, with the prevalence reported for its corpus.

    Args:
        key: Short identifier used in reports.
        title: The category's name.
        prevalence: Share of observed failures, as reported. A property of
            that corpus, not a forecast of yours.
        introduced: When in the system's life the failure was introduced.
    """

    key: str
    title: str
    prevalence: float
    introduced: str


@dataclass(frozen=True)
class Mode:
    """One named failure mode.

    Args:
        id: ``FM-<category>.<index>`` for MAST modes, ``LOCAL-*`` for ours.
        title: The mode's name, as the taxonomy states it.
        mechanism: One sentence naming what actually goes wrong. A mode
            without a stated mechanism cannot become a detector.
        northstar: What the mode looks like in this book's system.
        detectable: ``"structural"``, ``"state"``, ``"judge"``, or
            ``"unclassified"`` -- the cheapest tier that can recognise it.
    """

    id: str
    title: str
    mechanism: str
    northstar: str
    detectable: str


#: The three categories, and the prevalence reported for each.
CATEGORIES: tuple[Category, ...] = (
    Category(
        key="specification",
        title="Specification and system design issues",
        prevalence=0.418,
        introduced="design time",
    ),
    Category(
        key="misalignment",
        title="Inter-agent misalignment",
        prevalence=0.369,
        introduced="during execution, between agents",
    ),
    Category(
        key="verification",
        title="Task verification and termination",
        prevalence=0.213,
        introduced="at the end, when the work should have been checked",
    ),
)


#: The fourteen MAST modes: five, six, and three.
MAST_MODES: tuple[Mode, ...] = (
    # --------------------------------------------- 1. specification (5)
    Mode(
        "FM-1.1",
        "Disobey task specification",
        "The agent ignores a stated constraint on the task itself.",
        "Refunds a change-of-mind claim at 100% when the SKU override "
        "caps it at 50%.",
        "state",
    ),
    Mode(
        "FM-1.2",
        "Disobey role specification",
        "An agent acts outside the role it was given.",
        "The support agent decides a fraud case instead of handing it to "
        "the specialist.",
        "structural",
    ),
    Mode(
        "FM-1.3",
        "Step repetition",
        "The agent performs the same step again without new information.",
        "Calls get_order for the same order three times with identical "
        "arguments and nothing in between that could have invalidated it.",
        "structural",
    ),
    Mode(
        "FM-1.4",
        "Loss of conversation history",
        "Context the run depended on is no longer available to it.",
        "Re-asks the customer for the order number it was given four "
        "turns ago, after a compaction boundary.",
        "structural",
    ),
    Mode(
        "FM-1.5",
        "Unaware of termination conditions",
        "The agent does not know what done means, so it never stops or "
        "stops on a criterion it invented.",
        "Above-threshold claim with no terminal state encoded: the agent "
        "redrafts the apology until the turn budget raises.",
        "structural",
    ),
    # ------------------------------------------- 2. misalignment (6)
    Mode(
        "FM-2.1",
        "Conversation reset",
        "The exchange restarts and prior agreement is lost.",
        "The specialist re-opens the case from the beginning after the "
        "handoff, discarding what the customer already confirmed.",
        "structural",
    ),
    Mode(
        "FM-2.2",
        "Fail to ask for clarification",
        "The agent proceeds on an ambiguous instruction instead of asking.",
        "Refunds the first order on the account when the customer said "
        "only 'my last order'.",
        "judge",
    ),
    Mode(
        "FM-2.3",
        "Task derailment",
        "The work drifts away from the goal it was given.",
        "A damaged-item claim turns into a long exchange about delivery "
        "windows and no refund is ever issued.",
        "judge",
    ),
    Mode(
        "FM-2.4",
        "Information withholding",
        "An agent holds information another agent needed.",
        "escalate_to_specialist carries an empty note, so the specialist "
        "re-asks for the photo and the address the customer already gave.",
        "structural",
    ),
    Mode(
        "FM-2.5",
        "Ignored other agent's input",
        "A message arrives, is read, and changes nothing.",
        "The specialist replies that the order is under review and the "
        "support agent refunds it anyway.",
        "structural",
    ),
    Mode(
        "FM-2.6",
        "Reasoning-action mismatch",
        "The stated reasoning and the actual tool call diverge.",
        "The turn says 'I will check the policy first' and the call is "
        "issue_refund.",
        "judge",
    ),
    # -------------------------------------------- 3. verification (3)
    Mode(
        "FM-3.1",
        "Premature termination",
        "The run stops before the work is done.",
        "Sends a sympathetic message, issues no refund, and reports "
        "success.",
        "state",
    ),
    Mode(
        "FM-3.2",
        "No or incomplete verification",
        "Nothing checked the result through an independent path.",
        "issue_refund returns a receipt and nothing afterwards reads the "
        "refund ledger.",
        "structural",
    ),
    Mode(
        "FM-3.3",
        "Incorrect verification",
        "A verification step ran and reached the wrong conclusion.",
        "The run re-reads the order, sees two refund rows, and reports "
        "the ledger as balanced.",
        "state",
    ),
)


#: Northstar's own additions, after two rounds of labelling. Each one
#: recurred, has a mechanism that fits in a sentence, and would be fixed
#: differently from every MAST mode.
LOCAL_MODES: tuple[Mode, ...] = (
    Mode(
        "LOCAL-UNSAFE-SUCCESS",
        "Unsafe success",
        "The goal was reached through an action policy forbids.",
        "The refund was issued, and it was issued by bypassing the "
        "approval that policy requires above 5,000 cents.",
        "structural",
    ),
    Mode(
        "LOCAL-APPROVAL-BYPASS",
        "Approval bypass",
        "A gated action was taken without the gate ever being consulted.",
        "A 6,000-cent refund with no approval.requested and no "
        "approval.decided anywhere in the log.",
        "structural",
    ),
    Mode(
        "LOCAL-CROSS-TENANT-READ",
        "Cross-tenant read",
        "The run read a record belonging to a different customer.",
        "get_order on an order outside the requesting customer's account.",
        "structural",
    ),
)


#: Everything, keyed by id. ``catalog.py`` re-exports the short view of this.
MODES: dict[str, Mode] = {m.id: m for m in (*MAST_MODES, *LOCAL_MODES)}


def is_local(mode_id: str) -> bool:
    """Whether a mode id is a local extension rather than a MAST mode."""
    return mode_id.startswith("LOCAL-")


def category_of(mode_id: str) -> Category | None:
    """The category a MAST mode belongs to, or ``None`` for a local mode."""
    if is_local(mode_id) or not mode_id.startswith("FM-"):
        return None
    index = int(mode_id.split("-", 1)[1].split(".", 1)[0])
    return CATEGORIES[index - 1]


def modes_in(category_key: str) -> tuple[Mode, ...]:
    """Every MAST mode in one category."""
    return tuple(
        m for m in MAST_MODES
        if (c := category_of(m.id)) is not None and c.key == category_key
    )


def describe(mode_id: str) -> str:
    """One line naming a mode, for a report or a flag.

    Raises:
        KeyError: On an unknown mode. A label nobody can resolve is not a
            label, and failing loudly beats counting it.
    """
    if mode_id not in MODES:
        known = ", ".join(sorted(MODES))
        raise KeyError(f"unknown mode {mode_id!r}; known modes: {known}")
    return f"{mode_id} {MODES[mode_id].title}"
