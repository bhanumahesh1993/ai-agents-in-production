"""Graders: outcome, trajectory, and judge.

Three questions, three graders, and they are not interchangeable.

**Did the world end up right?** :class:`StateGrader` asks the authoritative
store, never the transcript. An agent that says "I have refunded you" and
did not is a failure that only a state grader catches, and it is the single
most common way a demo passes and production does not.

**Did it get there sensibly?** :class:`TrajectoryGrader` asks about the
path: which tools, in what order, how many times. A run that reaches the
right state by refunding twice and clawing one back is not a run you want
more of. So is a run that reads the policy nine times.

**Was the answer any good?** :class:`JudgeGrader` asks a model — or, in
mock mode, a deterministic rubric — about the things neither of the others
can see, like whether the customer was told the truth.

Outcome first, trajectory second, judge last. A judge that disagrees with
the world is wrong; a judge that agrees with the world tells you nothing
new. Its value lies in the space the other two do not cover.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from northstar_contracts import Message, RunState, World

__all__ = [
    "GradeResult",
    "Grader",
    "JudgeGrader",
    "StateGrader",
    "TrajectoryGrader",
    "grade_all",
    "tool_calls_of",
    "trajectory",
]


@dataclass(frozen=True)
class GradeResult:
    """The verdict from one grader.

    Args:
        passed: The bit a release gate reads.
        score: ``0.0``-``1.0``. Partial credit is for diagnosis, not for
            gating: a run that half-refunded a customer did not half pass.
        reasons: Human-readable findings, failures first.
        grader: Which grader produced this.
        details: Anything a dashboard or a triage session might want.
    """

    passed: bool
    score: float
    reasons: list[str] = field(default_factory=list)
    grader: str = ""
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """JSON-serialisable form."""
        return {
            "passed": self.passed,
            "score": round(self.score, 6),
            "reasons": list(self.reasons),
            "grader": self.grader,
            "details": dict(self.details),
        }


@runtime_checkable
class Grader(Protocol):
    """Anything that can judge a finished run."""

    def grade(self, run: RunState, world: World) -> GradeResult:
        """Return a verdict on one run."""
        ...


def tool_calls_of(run: RunState) -> list[tuple[str, dict[str, Any]]]:
    """Every tool call the model made, in order, with its arguments."""
    calls: list[tuple[str, dict[str, Any]]] = []
    for message in run.messages:
        for call in message.tool_calls:
            calls.append((call.name, dict(call.arguments)))
    return calls


def trajectory(run: RunState) -> list[str]:
    """The sequence of tool names the run called.

    The trajectory is recovered from the messages rather than tracked
    separately, so it stays true across a checkpoint, a resume, and a
    replay. A trajectory kept in a side channel is a trajectory that
    disagrees with the transcript after the first restart.
    """
    return [name for name, _ in tool_calls_of(run)]


def observations_of(run: RunState) -> list[dict[str, Any]]:
    """Every tool observation the model saw, in order."""
    return [
        m.content
        for m in run.messages
        if m.role == "tool" and isinstance(m.content, dict)
    ]


@dataclass(frozen=True)
class _Check:
    """One named assertion."""

    name: str
    predicate: Callable[[Any], bool]
    message: str = ""


class StateGrader:
    """Asserts on the authoritative world, not on what the agent said.

    Args:
        checks: ``(name, predicate)`` pairs. Each predicate receives the
            :class:`~northstar_contracts.world.World`.
        require_all: Fail unless every check passes. Leave this on. An
            outcome grader that passes on three of five conditions is a
            grader that will let a half-completed refund into production.

    Example:
        >>> grader = StateGrader().refunded("NR-2026-0041903", 3250)
        >>> grader.grade(run, world).passed
        True
    """

    name = "state"

    def __init__(
        self,
        checks: Sequence[tuple[str, Callable[[World], bool]]] = (),
        *,
        require_all: bool = True,
    ) -> None:
        self._checks: list[_Check] = [
            _Check(name, predicate) for name, predicate in checks
        ]
        self.require_all = require_all

    def check(
        self,
        name: str,
        predicate: Callable[[World], bool],
        message: str = "",
    ) -> StateGrader:
        """Add an arbitrary check. Returns ``self``, for chaining."""
        self._checks.append(_Check(name, predicate, message))
        return self

    # ------------------------------------------------- Northstar shorthands

    def refunded(self, order_id: str, cents: int) -> StateGrader:
        """The order's total refunded amount is exactly ``cents``."""
        return self.check(
            f"refunded({order_id})=={cents}",
            lambda w: w.total_refunded_cents(order_id) == cents,
            f"expected {cents}c refunded against {order_id}",
        )

    def no_duplicate_refunds(self, order_id: str) -> StateGrader:
        """At most one refund row exists for the order.

        The Chapter 1 assertion. Note that it is about *rows*, not totals:
        two refunds of 4200c and a total of 8400c is not the same failure
        as one refund of 8400c, and only the row count tells them apart.
        """
        return self.check(
            f"single_refund_row({order_id})",
            lambda w: len(w.refunds_for(order_id)) <= 1,
            f"more than one refund row exists for {order_id}",
        )

    def messages_sent(self, count: int) -> StateGrader:
        """Exactly ``count`` customer-visible messages were sent."""
        return self.check(
            f"messages_sent=={count}",
            lambda w: len(w.messages) == count,
            f"expected exactly {count} customer message(s)",
        )

    def escalated(self, order_id: str) -> StateGrader:
        """An open specialist case exists for the order."""
        return self.check(
            f"escalated({order_id})",
            lambda w: any(
                c["order_id"] == order_id and c["status"] == "open"
                for c in w.escalations
            ),
            f"expected an open specialist case for {order_id}",
        )

    def no_writes(self) -> StateGrader:
        """Nothing was written at all. The read-only agent's contract."""
        return self.check(
            "no_side_effects",
            lambda w: not w.ledger,
            "expected no entries in the side-effect ledger",
        )

    # ------------------------------------------------------------- grading

    def grade(self, run: RunState, world: World) -> GradeResult:
        """Run every check against the world."""
        passes: list[bool] = []
        reasons: list[str] = []
        for check in self._checks:
            try:
                ok = bool(check.predicate(world))
            except Exception as exc:  # noqa: BLE001 - a broken check fails
                ok = False
                reasons.append(f"{check.name}: raised {type(exc).__name__}")
            else:
                if not ok:
                    reasons.append(check.message or f"{check.name}: failed")
            passes.append(ok)

        if not passes:
            return GradeResult(
                passed=False,
                score=0.0,
                reasons=["no checks configured"],
                grader=self.name,
            )
        score = sum(passes) / len(passes)
        passed = all(passes) if self.require_all else any(passes)
        return GradeResult(
            passed=passed,
            score=score,
            reasons=reasons,
            grader=self.name,
            details={
                "checks": len(passes),
                "passed_checks": sum(passes),
                "world": world.snapshot(),
            },
        )


class TrajectoryGrader:
    """Asserts on the sequence of tool calls, not on the final state.

    Args:
        required: Tool names that must appear, in this relative order.
            Other calls may appear between them: an agent that reads twice
            before refunding is following a valid alternative path, and a
            grader that insists on an exact sequence rejects every one of
            them.
        forbidden: Tool names that must not appear at all.
        max_calls: Ceiling on total tool calls. Catches the loop that gets
            there eventually and expensively.
        max_repeats: Ceiling on consecutive identical tool names. This is
            the step-repetition failure mode from Chapter 16.
        before: ``(a, b)`` pairs requiring every ``a`` to precede the
            first ``b``. ``("get_policy", "issue_refund")`` is the one the
            book uses: check the rules before you move the money.
    """

    name = "trajectory"

    def __init__(
        self,
        required: Sequence[str] = (),
        forbidden: Sequence[str] = (),
        *,
        max_calls: int | None = None,
        max_repeats: int | None = None,
        before: Sequence[tuple[str, str]] = (),
    ) -> None:
        self.required = list(required)
        self.forbidden = list(forbidden)
        self.max_calls = max_calls
        self.max_repeats = max_repeats
        self.before = list(before)

    def grade(self, run: RunState, world: World) -> GradeResult:
        """Check the run's tool sequence against every configured rule."""
        path = trajectory(run)
        reasons: list[str] = []
        checks = 0
        passed_checks = 0

        if self.required:
            checks += 1
            missing = _first_missing(self.required, path)
            if missing is None:
                passed_checks += 1
            else:
                reasons.append(
                    f"required tool {missing!r} missing or out of order; "
                    f"trajectory was {path}"
                )

        for tool in self.forbidden:
            checks += 1
            if tool in path:
                reasons.append(f"forbidden tool {tool!r} was called")
            else:
                passed_checks += 1

        if self.max_calls is not None:
            checks += 1
            if len(path) > self.max_calls:
                reasons.append(
                    f"{len(path)} tool calls exceeds the budget of "
                    f"{self.max_calls}"
                )
            else:
                passed_checks += 1

        if self.max_repeats is not None:
            checks += 1
            worst, tool = _longest_run(path)
            if worst > self.max_repeats:
                reasons.append(
                    f"{tool!r} called {worst} times in a row, over the "
                    f"limit of {self.max_repeats} (step repetition)"
                )
            else:
                passed_checks += 1

        for earlier, later in self.before:
            checks += 1
            if _precedes(path, earlier, later):
                passed_checks += 1
            else:
                reasons.append(
                    f"{earlier!r} must be called before {later!r}"
                )

        if checks == 0:
            return GradeResult(
                False, 0.0, ["no checks configured"], self.name
            )
        return GradeResult(
            passed=passed_checks == checks,
            score=passed_checks / checks,
            reasons=reasons,
            grader=self.name,
            details={"trajectory": path, "calls": len(path)},
        )


def _first_missing(required: Sequence[str], path: Sequence[str]) -> str | None:
    """The first required tool that is absent or out of order."""
    cursor = 0
    for tool in required:
        try:
            cursor = path.index(tool, cursor) + 1
        except ValueError:
            return tool
    return None


def _longest_run(path: Sequence[str]) -> tuple[int, str]:
    """The longest streak of one repeated tool name, and which tool."""
    best, best_tool = 0, ""
    current, current_tool = 0, ""
    for tool in path:
        if tool == current_tool:
            current += 1
        else:
            current, current_tool = 1, tool
        if current > best:
            best, best_tool = current, tool
    return best, best_tool


def _precedes(path: Sequence[str], earlier: str, later: str) -> bool:
    """Whether ``earlier`` appears before the first ``later``."""
    if later not in path:
        return True
    return earlier in path and path.index(earlier) < path.index(later)


#: The rubric a judge scores against. Each entry is a criterion name and
#: the keywords whose presence (or absence) satisfies it in mock mode.
JudgeRubric = dict[str, dict[str, Any]]

DEFAULT_RUBRIC: JudgeRubric = {
    "states_the_outcome": {
        "any_of": ["refund", "escalat", "unable", "cannot", "specialist"],
        "weight": 1.0,
        "description": "The reply says what actually happened.",
    },
    "quantifies": {
        "any_of": ["$", "cents", "usd", "us$"],
        "weight": 1.0,
        "description": "The reply names an amount rather than gesturing.",
    },
    "no_unhedged_promise": {
        "none_of": ["guarantee", "definitely will", "i promise"],
        "weight": 1.0,
        "description": "The reply promises nothing it cannot control.",
    },
}


class JudgeGrader:
    """LLM-as-judge, deterministic in mock mode.

    In mock mode the "judge" is the rubric below: keyword criteria over the
    agent's final answer. That is not a language model and does not pretend
    to be one. It exists so the *plumbing* around a judge — rubric,
    threshold, per-criterion reporting, calibration against a labelled
    set — can be exercised, tested, and taught offline, and so the judge
    can be swapped for a real model by passing ``model``.

    Calibrate any real judge against human labels before you trust it. An
    uncalibrated judge is a random number generator with good manners.

    Args:
        rubric: Criteria to score. Defaults to :data:`DEFAULT_RUBRIC`.
        threshold: Fraction of weighted criteria needed to pass.
        model: Optional provider. When given, the judge asks it and parses
            a ``PASS``/``FAIL`` verdict from the reply.
    """

    name = "judge"

    def __init__(
        self,
        rubric: JudgeRubric | None = None,
        *,
        threshold: float = 0.67,
        model: Any | None = None,
    ) -> None:
        self.rubric = dict(rubric or DEFAULT_RUBRIC)
        self.threshold = threshold
        self.model = model

    def grade(self, run: RunState, world: World) -> GradeResult:
        """Score the run's final answer against the rubric."""
        answer = (run.final_text or "").strip()
        if self.model is not None:
            return self._grade_with_model(answer, run)
        return self._grade_with_rubric(answer)

    def _grade_with_rubric(self, answer: str) -> GradeResult:
        """The deterministic mock-mode judge."""
        lowered = answer.lower()
        reasons: list[str] = []
        earned = 0.0
        total = 0.0
        per_criterion: dict[str, bool] = {}

        for criterion, spec in self.rubric.items():
            weight = float(spec.get("weight", 1.0))
            total += weight
            any_of = [k.lower() for k in spec.get("any_of", [])]
            none_of = [k.lower() for k in spec.get("none_of", [])]
            ok = True
            if any_of and not any(k in lowered for k in any_of):
                ok = False
            if none_of and any(k in lowered for k in none_of):
                ok = False
            per_criterion[criterion] = ok
            if ok:
                earned += weight
            else:
                reasons.append(
                    f"{criterion}: {spec.get('description', 'not satisfied')}"
                )

        score = earned / total if total else 0.0
        return GradeResult(
            passed=score >= self.threshold,
            score=score,
            reasons=reasons,
            grader=self.name,
            details={
                "criteria": per_criterion,
                "threshold": self.threshold,
                "mode": "rubric",
                "answer_chars": len(answer),
            },
        )

    def _grade_with_model(self, answer: str, run: RunState) -> GradeResult:
        """Ask a real model. Only reached when ``model`` was supplied."""
        prompt = (
            "You are grading a customer support agent's final reply.\n"
            "Criteria:\n"
            + "\n".join(
                f"- {name}: {spec.get('description', name)}"
                for name, spec in self.rubric.items()
            )
            + f"\n\nReply to grade:\n{answer}\n\n"
            "Answer with PASS or FAIL on the first line, then one short "
            "sentence of justification."
        )
        if self.model is None:
            raise ValueError(
                f"{self.name}: model-backed judging was requested but no "
                "model was supplied. Pass model=..., or use the "
                "deterministic rubric path."
            )
        response = self.model.complete(
            [Message(role="user", content=prompt)], []
        )
        verdict = (response.text or "").strip()
        passed = verdict.upper().startswith("PASS")
        return GradeResult(
            passed=passed,
            score=1.0 if passed else 0.0,
            reasons=[] if passed else [verdict],
            grader=self.name,
            details={"mode": "model", "verdict": verdict},
        )


def grade_all(
    graders: Sequence[Grader],
    run: RunState,
    world: World,
) -> GradeResult:
    """Run several graders and combine them conjunctively.

    All must pass. Combining graders with "or" produces a suite that gets
    easier as you add checks to it, which is the wrong direction.
    """
    results = [g.grade(run, world) for g in graders]
    reasons = [
        f"[{r.grader}] {reason}" for r in results for reason in r.reasons
    ]
    score = sum(r.score for r in results) / len(results) if results else 0.0
    return GradeResult(
        passed=all(r.passed for r in results) and bool(results),
        score=score,
        reasons=reasons,
        grader="+".join(r.grader for r in results),
        details={"results": [r.to_dict() for r in results]},
    )
