"""A benchmark task is a record, not a prompt.

Everything a grader needs is declared up front: the fixtures the task starts
from, the user's script, the steps only the user can perform, the success
predicate over authoritative state, the actions that are forbidden however
happy the customer ends up, and a budget in turns and cents. Two extra fields
that a public benchmark usually leaves implicit are explicit here, because
Chapter 14 argues they decide whether the number means anything: ``split``,
so a frozen holdout exists, and ``provenance``, so every task can be traced
to the ticket or incident it came from.

The set ships as JSON rather than as Python because the shape is the point.
A task set you can hand to another harness is a task set you can still trust
after you change harnesses, and it is the shape a public benchmark publishes.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from northstar_contracts import Money

__all__ = [
    "TASK_FILE",
    "BenchmarkTask",
    "dual_control",
    "holdout",
    "load_tasks",
    "solo",
    "train",
]

#: The shipped task set. Forty tasks, eleven of them dual control.
TASK_FILE = Path(__file__).resolve().parent / "northstar_tasks.json"


@dataclass(frozen=True)
class BenchmarkTask:
    """One task in the Northstar internal benchmark.

    Args:
        task_id: Stable identifier. It is the join key for every report.
        goal: What the customer opens with.
        initial_orders: World fixture ids the task starts from. Anything
            not listed is removed, so a task cannot pass by touching an
            order it was never given.
        user_script: The simulated user's follow-up turns.
        user_actions: Steps only the customer can perform. A task with a
            non-empty list is a dual-control task, and no write completes
            until the customer has actually done what was asked.
        reason: Refund reason the case falls under, or ``""`` for a
            read-only question.
        sku: The product under discussion, or ``""``.
        expected_refund_cents: Graded against the refund ledger, never
            against the assistant's final message. Zero means "no money
            should move", which is a real expectation and not an absence
            of one.
        expected_order_id: The order every expectation is asserted on.
        expect_escalation: Whether an open specialist case is required.
        forbidden_tools: Actions that make the run a failure whatever the
            final state looks like. This is what separates a safe failure
            from an unsafe success.
        max_turns: Turn ceiling for the run.
        budget_cents: Money ceiling for the run.
        split: ``"train"`` or ``"holdout"``. The holdout is frozen and is
            not looked at while prompts are being iterated on.
        provenance: Which ticket or incident this task came from. A task
            with no provenance is a task nobody can defend in review.
    """

    task_id: str
    goal: str
    initial_orders: list[str]
    user_script: list[str]
    user_actions: list[str] = field(default_factory=list)
    reason: str = ""
    sku: str = ""
    expected_refund_cents: Money = 0
    expected_order_id: str = ""
    expect_escalation: bool = False
    forbidden_tools: frozenset[str] = frozenset()
    max_turns: int = 12
    budget_cents: Money = 200
    split: str = "train"
    provenance: str = ""

    @property
    def is_dual_control(self) -> bool:
        """Whether the world only changes if the customer acts too."""
        return bool(self.user_actions)

    @property
    def primary_order(self) -> str:
        """The order the task is about."""
        return self.expected_order_id or self.initial_orders[0]

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> BenchmarkTask:
        """Build one task from its JSON record.

        Raises:
            ValueError: If a required field is missing or the expectation
                is not expressible against the declared fixtures. A task
                whose expected order is not in its own fixture list would
                fail for a reason that has nothing to do with the agent.
        """
        for required in ("task_id", "goal", "initial_orders"):
            if not data.get(required):
                raise ValueError(
                    f"benchmark task is missing {required!r}: {data}"
                )
        task = cls(
            task_id=str(data["task_id"]),
            goal=str(data["goal"]),
            initial_orders=list(data["initial_orders"]),
            user_script=list(data.get("user_script", [])),
            user_actions=list(data.get("user_actions", [])),
            reason=str(data.get("reason", "")),
            sku=str(data.get("sku", "")),
            expected_refund_cents=int(data.get("expected_refund_cents", 0)),
            expected_order_id=str(data.get("expected_order_id", "")),
            expect_escalation=bool(data.get("expect_escalation", False)),
            forbidden_tools=frozenset(data.get("forbidden_tools", [])),
            max_turns=int(data.get("max_turns", 12)),
            budget_cents=int(data.get("budget_cents", 200)),
            split=str(data.get("split", "train")),
            provenance=str(data.get("provenance", "")),
        )
        if task.primary_order not in task.initial_orders:
            raise ValueError(
                f"{task.task_id}: expected order {task.primary_order!r} is "
                "not among the task's own fixtures"
            )
        if task.expected_refund_cents and "issue_refund" in (
            task.forbidden_tools
        ):
            raise ValueError(
                f"{task.task_id}: a task cannot both require a refund and "
                "forbid the refund tool"
            )
        return task

    def to_dict(self) -> dict[str, Any]:
        """The JSON record, for round-tripping and for a CI artifact."""
        return {
            "task_id": self.task_id,
            "goal": self.goal,
            "initial_orders": list(self.initial_orders),
            "user_script": list(self.user_script),
            "user_actions": list(self.user_actions),
            "reason": self.reason,
            "sku": self.sku,
            "expected_refund_cents": self.expected_refund_cents,
            "expected_order_id": self.expected_order_id,
            "expect_escalation": self.expect_escalation,
            "forbidden_tools": sorted(self.forbidden_tools),
            "max_turns": self.max_turns,
            "budget_cents": self.budget_cents,
            "split": self.split,
            "provenance": self.provenance,
        }


def load_tasks(path: Path | None = None) -> list[BenchmarkTask]:
    """Load the shipped task set.

    Raises:
        ValueError: If the file holds no tasks, which is the failure a
            silent empty suite would otherwise hide behind a 100% score.
    """
    source = path or TASK_FILE
    document = json.loads(source.read_text(encoding="utf-8"))
    tasks = [BenchmarkTask.from_dict(row) for row in document["tasks"]]
    if not tasks:
        raise ValueError(f"{source} declares no tasks")
    return tasks


def holdout(tasks: list[BenchmarkTask]) -> list[BenchmarkTask]:
    """The frozen split, not looked at while iterating on prompts."""
    return [t for t in tasks if t.split == "holdout"]


def train(tasks: list[BenchmarkTask]) -> list[BenchmarkTask]:
    """Everything outside the holdout."""
    return [t for t in tasks if t.split != "holdout"]


def dual_control(tasks: list[BenchmarkTask]) -> list[BenchmarkTask]:
    """Tasks where the world only changes if the customer acts."""
    return [t for t in tasks if t.is_dual_control]


def solo(tasks: list[BenchmarkTask]) -> list[BenchmarkTask]:
    """Tasks the agent can finish on its own."""
    return [t for t in tasks if not t.is_dual_control]
