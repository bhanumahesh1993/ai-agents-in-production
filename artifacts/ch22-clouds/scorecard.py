"""One scorecard, so three evaluations are comparable by construction.

Three engineers can each do real work and produce three numbers nobody can
compare, because they deployed three different systems. The antidote is
mechanical: the same agent, the same task set, the same auth boundary, the
same success definition, and the same recorded fields.

Two fields carry most of the value.

``cold_start_ms`` is ``None`` rather than a vendor figure when the team did
not measure it, because **a missing measurement is information and a
borrowed one is not**.

``cents_per_verified_success`` divides by *graded* successes, not by
invocations. A platform that is cheap per call and fails more often is not
cheap. When nothing was graded a success the field is
:data:`UNDEFINED_COST` rather than a number, because a cost per success
with no successes under it is arithmetic pretending to be a measurement.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from adapters.base import ADAPTER_METHODS, CloudAdapter, extra_methods
from northstar_evals import pass_k
from northstar_telemetry import CostLedger
from portable import Attempt, run_once
from tasks import TASKS, Task

__all__ = [
    "UNDEFINED_COST",
    "CloudScore",
    "compare",
    "render",
    "score",
]

#: What ``cents_per_verified_success`` holds when nothing was verified.
#: Not zero, which would read as free, and not the total, which would read
#: as a rate.
UNDEFINED_COST = -1


@dataclass(frozen=True)
class CloudScore:
    """The fields every platform reports, and only those."""

    cloud: str
    region: str
    verified_success_rate: float   # graded against authoritative state
    pass_k: float                  # k repetitions, from Chapter 13
    p50_ms: int
    p95_ms: int
    cold_start_ms: int | None      # None when not measured, never guessed
    cents_per_verified_success: int
    preview_dependencies: int
    non_negotiables_met: bool

    def to_dict(self) -> dict[str, Any]:
        """JSON-serialisable form, for a decision record."""
        return {
            "cloud": self.cloud,
            "region": self.region,
            "verified_success_rate": round(self.verified_success_rate, 4),
            "pass_k": round(self.pass_k, 4),
            "p50_ms": self.p50_ms,
            "p95_ms": self.p95_ms,
            "cold_start_ms": self.cold_start_ms,
            "cents_per_verified_success": self.cents_per_verified_success,
            "preview_dependencies": self.preview_dependencies,
            "non_negotiables_met": self.non_negotiables_met,
        }


def percentile(values: list[int], fraction: float) -> int:
    """Nearest-rank percentile over measured durations."""
    if not values:
        return 0
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, int(round(fraction * len(ordered))) - 1))
    return ordered[index]


def score(
    adapter: CloudAdapter,
    tasks: tuple[Task, ...] = TASKS,
    *,
    k: int = 5,
    region: str = "",
    ledger: CostLedger | None = None,
) -> tuple[CloudScore, list[Attempt]]:
    """Run the task set ``k`` times on one adapter and report one score.

    Args:
        adapter: The platform under test.
        tasks: Held identical across platforms, or the numbers are not
            comparable.
        k: Repetitions per task. ``pass^k`` is the release number; a single
            pass is the headline one.
        region: Reported verbatim. Residency is a decision, not a default.
        ledger: Cost model. Defaults to one with an empty price table, so
            every model falls through to the repository's illustrative
            placeholder rather than to a rate anybody might mistake for a
            quote.

    Returns:
        The score and every attempt behind it, so a reader can check the
        arithmetic rather than trusting the row.
    """
    prices = ledger if ledger is not None else CostLedger(prices={})
    attempts: list[Attempt] = []
    per_task: dict[str, list[bool]] = {}

    for repetition in range(k):
        for task in tasks:
            attempt = run_once(
                adapter,
                task.name,
                task.goal,
                task.script,
                task.grader,
                run_id=f"{adapter.name}-{task.name}-{repetition}",
            )
            attempts.append(attempt)
            per_task.setdefault(task.name, []).append(attempt.verified)
            prices.record(
                attempt.model,
                attempt.input_tokens,
                attempt.output_tokens,
                run_id=f"{adapter.name}-{task.name}-{repetition}",
            )

    verified = sum(1 for a in attempts if a.verified)
    total_cents = prices.total_cents()
    durations = [a.duration_ms for a in attempts]
    non_negotiable = {t.name for t in tasks if t.non_negotiable}

    return (
        CloudScore(
            cloud=adapter.name,
            region=region or getattr(adapter, "region", ""),
            verified_success_rate=verified / len(attempts) if attempts else 0.0,
            pass_k=(
                sum(pass_k(results, k) for results in per_task.values())
                / len(per_task)
                if per_task
                else 0.0
            ),
            p50_ms=percentile(durations, 0.50),
            p95_ms=percentile(durations, 0.95),
            # Asked of the adapter, which answers None unless someone
            # actually measured one.
            cold_start_ms=_ask(adapter, "cold_start_ms"),
            cents_per_verified_success=(
                -(-total_cents // verified) if verified else UNDEFINED_COST
            ),
            preview_dependencies=_ask(adapter, "preview_dependencies") or 0,
            non_negotiables_met=all(
                all(per_task.get(name, [False]))
                for name in non_negotiable
            ),
        ),
        attempts,
    )


def _ask(adapter: CloudAdapter, method: str) -> Any:
    """Ask an adapter for an optional scorecard input.

    These are not part of the four-method interface, deliberately. The loop
    never calls them, so they cannot become load-bearing without someone
    noticing, and an adapter that does not answer reports ``None`` rather
    than a default that would read as a measurement.
    """
    fn = getattr(adapter, method, None)
    return fn() if callable(fn) else None


def compare(scores: list[CloudScore]) -> list[str]:
    """Reasons a platform is out, whatever its aggregate score.

    Reject a platform if a non-negotiable control scores zero, even when
    its total is highest. An excellent platform that cannot meet your
    residency requirement is not a compromise; it is a rewrite scheduled
    for later.
    """
    out: list[str] = []
    for entry in scores:
        if not entry.non_negotiables_met:
            out.append(
                f"{entry.cloud}: a non-negotiable control did not hold"
            )
        if entry.cents_per_verified_success == UNDEFINED_COST:
            out.append(
                f"{entry.cloud}: no verified successes, so cost per success "
                f"is undefined rather than zero"
            )
    return out


def interface_drift(adapters: list[CloudAdapter]) -> dict[str, list[str]]:
    """Methods each adapter has beyond the four the core calls.

    A non-empty list is a number for the exit-cost note rather than a
    failure. The check that matters is the one in the test suite: the
    portable core must call exactly :data:`ADAPTER_METHODS` and nothing
    else, whatever the adapters happen to expose.
    """
    return {a.name: extra_methods(a) for a in adapters}


def render(scores: list[CloudScore]) -> str:
    """The scorecard as a table, with unmeasured fields shown as such."""
    header = (
        f"{'cloud':<8} {'region':<14} {'verified':>8} {'pass^k':>7} "
        f"{'p50':>5} {'p95':>5} {'cold':>6} {'c/succ':>7} "
        f"{'prev':>5} {'ok':>4}"
    )
    lines = [header, "-" * len(header)]
    for entry in scores:
        cold = "n/a" if entry.cold_start_ms is None else str(entry.cold_start_ms)
        cost = (
            "n/a"
            if entry.cents_per_verified_success == UNDEFINED_COST
            else str(entry.cents_per_verified_success)
        )
        lines.append(
            f"{entry.cloud:<8} {entry.region:<14} "
            f"{entry.verified_success_rate:>8.2f} {entry.pass_k:>7.2f} "
            f"{entry.p50_ms:>5} {entry.p95_ms:>5} {cold:>6} {cost:>7} "
            f"{entry.preview_dependencies:>5} "
            f"{'yes' if entry.non_negotiables_met else 'NO':>4}"
        )
    lines.append("")
    lines.append(
        "cold: milliseconds, n/a when nobody measured one. Never a vendor "
        "figure."
    )
    lines.append(
        "c/succ: cents per *verified* success, graded against the world. "
        "n/a when nothing was verified."
    )
    lines.append(f"the core calls exactly: {', '.join(ADAPTER_METHODS)}")
    return "\n".join(lines)
