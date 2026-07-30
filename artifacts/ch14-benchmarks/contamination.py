"""A cheap guard against rebuilding a public benchmark under a private name.

The internal task set earns its place by being uncontaminated *by
construction*: it never leaves the repository, so it cannot appear in a
training corpus or be tuned against by someone else's scaffold. That argument
survives exactly as long as nobody pastes a public task into it, which happens
by accident more often than anyone admits when a team is assembling forty
cases in a hurry.

This is not a plagiarism detector and does not pretend to be. It is a shingle
overlap check against a small local corpus of phrasings lifted from public
agent benchmarks and their papers. It catches copy-paste. It does not catch
paraphrase, and a clean report here is not evidence that your set is private
in the sense that matters -- for that, read the retention and training terms
of the exact endpoint your harness calls.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from task import BenchmarkTask

__all__ = [
    "PUBLIC_SNIPPETS",
    "SHINGLE",
    "ContaminationHit",
    "check_tasks",
    "shingles",
]

#: Word count of the overlapping window. Four is short enough to catch a
#: lifted sentence and long enough that ordinary English does not trip it.
SHINGLE = 4

#: Phrasings drawn from published agent benchmarks and their task templates.
#: Deliberately small and local: the point is a guard you can read, not a
#: corpus you have to host.
PUBLIC_SNIPPETS: tuple[str, ...] = (
    "you are a retail agent helping a user with their order",
    "the user wants to exchange an item for a different size",
    "resolve the issue described in the following github issue",
    "the assistant must follow the domain policy at all times",
    "you may only take one action per turn and must confirm first",
    "book a flight from san francisco to new york for the user",
    "find the answer using the tools provided and reply with the answer",
    "modify the repository so that the failing tests pass",
    "the user has a hidden goal that you must elicit through dialogue",
    "complete the task in the terminal and verify with the checker",
)


@dataclass(frozen=True)
class ContaminationHit:
    """One task that shares wording with the public corpus."""

    task_id: str
    field: str
    overlap: tuple[str, ...]

    def describe(self) -> str:
        """One line for the report."""
        joined = " | ".join(" ".join(w) for w in [self.overlap])
        return f"{self.task_id}.{self.field}: {joined!r}"


def _normalise(text: str) -> list[str]:
    """Lowercase, strip punctuation, split on whitespace."""
    return re.sub(r"[^a-z0-9 ]+", " ", text.lower()).split()


def shingles(text: str, size: int = SHINGLE) -> set[tuple[str, ...]]:
    """Every ``size``-word window in ``text``."""
    words = _normalise(text)
    if len(words) < size:
        return set()
    return {
        tuple(words[i : i + size]) for i in range(len(words) - size + 1)
    }


def _public_shingles(size: int = SHINGLE) -> set[tuple[str, ...]]:
    """Every window in the local public corpus."""
    out: set[tuple[str, ...]] = set()
    for snippet in PUBLIC_SNIPPETS:
        out |= shingles(snippet, size)
    return out


def check_tasks(
    tasks: list[BenchmarkTask],
    size: int = SHINGLE,
) -> list[ContaminationHit]:
    """Report tasks whose text overlaps the public corpus verbatim.

    Both the goal and every user turn are checked, because a task set is
    usually assembled by pasting the customer's side of a conversation and
    that is the side most likely to have been copied.
    """
    public = _public_shingles(size)
    hits: list[ContaminationHit] = []
    for task in tasks:
        fields = [("goal", task.goal)]
        fields += [
            (f"user_script[{i}]", line)
            for i, line in enumerate(task.user_script)
        ]
        for name, text in fields:
            shared = shingles(text, size) & public
            for window in sorted(shared):
                hits.append(ContaminationHit(task.task_id, name, window))
    return hits
