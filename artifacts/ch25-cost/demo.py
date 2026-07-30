"""Measure four cost configurations on one task set and grade all of them.

    python artifacts/ch25-cost/demo.py

Four configurations over the same six Northstar tickets: baseline, cached,
routed, and cached-plus-routed. The demo prints tokens, cached tokens,
modelled p50 and p95 latency, verified success rate, and cents per verified
success for each, then shows three properties of the machinery: a stable
prompt prefix against an unstable one, a cache that cannot serve one tenant
from another's entry, and a budget that fails closed.

The routed configuration is deliberately unflattering. Its cost *per call*
falls and its cost *per verified success* rises, because the step it sends
to the cheap model is the one where the cheap model applies the refund
policy wrongly — well formed, arithmetically consistent, and 1,625 cents
too generous. No deterministic check can see that, which is why the number
that decides a routing question is cost per graded success and not price
per token.

Exits non-zero if the routed configuration does not reproduce that
inversion, if caching does not reduce spend, if an unstable prefix still
gets cache hits, if one tenant's prefix warms another's cache, or if the
per-run budget does not raise.

All prices are illustrative placeholders. Latency is modelled from the
declared table in ``compare.py``, not measured.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from budgets import (  # noqa: E402
    CACHE_READ_MULTIPLIER,
    HUMAN_HANDLING_CENTS,
    HUMAN_HANDLING_SECONDS,
    ILLUSTRATIVE_PRICES,
    budgeted_loop,
)
from cache import PrefixCache, assemble, cache_key  # noqa: E402
from compare import ConfigReport, MeteredModel, measure_all  # noqa: E402
from northstar_contracts import Message, World  # noqa: E402
from northstar_policy import BudgetExceeded, Principal  # noqa: E402
from northstar_runtime import ToolRegistry  # noqa: E402
from scenarios import DAMAGED_LAMP_SHADE, SCENARIOS  # noqa: E402

TENANTS = ("acme-support", "globex-support")


def report_cost(reports: list[ConfigReport]) -> None:
    """Print tokens and spend, one row per configuration."""
    print("\n=== tokens and model spend ===")
    print(
        f"{'config':<14}{'calls':>6}{'input':>9}{'cached':>9}"
        f"{'output':>8}{'cents':>7}{'c/call':>8}"
    )
    for r in reports:
        print(
            f"{r.label:<14}{r.model_calls:>6}{r.input_tokens:>9}"
            f"{r.cached_tokens:>9}{r.output_tokens:>8}"
            f"{r.model_cents:>7}{r.cents_per_call:>8.3f}"
        )


def report_outcome(reports: list[ConfigReport]) -> None:
    """Print graded outcomes, modelled latency, and cost per success."""
    print("\n=== verified outcomes, latency, and cost per success ===")
    print(
        f"{'config':<14}{'ok':>4}{'rate':>7}{'p50 ms':>9}{'p95 ms':>9}"
        f"{'esc':>5}{'human c':>9}{'c/success':>11}"
    )
    for r in reports:
        print(
            f"{r.label:<14}{r.successes:>2}/{len(r.runs):<2}"
            f"{r.verified_success_rate:>6.0%}{r.p50_ms:>9.0f}"
            f"{r.p95_ms:>9.0f}{r.escalations:>5}{r.human_cents:>9}"
            f"{r.cents_per_success:>11.2f}"
        )


def report_failures(reports: list[ConfigReport]) -> None:
    """Name every run a state grader rejected, and why."""
    print("\n=== what the graders rejected ===")
    for r in reports:
        for run in r.runs:
            if run.passed and not run.notes:
                continue
            for note in run.notes:
                print(f"  {r.label:<14} {run.scenario:<20} note: {note}")
            if not run.passed:
                reason = run.grade.reasons[0] if run.grade.reasons else "?"
                print(f"  {r.label:<14} {run.scenario:<20} FAIL: {reason}")


def prefix_stability() -> tuple[float, float]:
    """Hit rate with a stable prefix, and with a run id at the front."""
    world = World()
    specs = world.tool_specs()
    conversation = [Message(role="user", content="where is my order")]

    rates: list[float] = []
    for stable in (True, False):
        cache = PrefixCache()
        for index in range(6):
            prompt = assemble(
                "You are the Northstar Returns support agent.",
                specs,
                conversation,
                reference="Refund policy revision 2026-07-01.",
                run_marker="" if stable else f"run-{index:04d}",
            )
            cache.lookup("acme-support", prompt.prefix)
        rates.append(cache.hit_rate)
    return rates[0], rates[1]


def tenant_isolation() -> tuple[int, int, bool]:
    """Warm one tenant's prefix and try to read it as another tenant."""
    world = World()
    specs = world.tool_specs()
    prompt = assemble(
        "You are the Northstar Returns support agent.",
        specs,
        [Message(role="user", content="where is my order")],
    )
    cache = PrefixCache()
    for tenant in TENANTS:
        cache.lookup(tenant, prompt.prefix)
    keys_differ = cache_key(TENANTS[0], prompt.prefix) != cache_key(
        TENANTS[1], prompt.prefix
    )
    return cache.hits, cache.misses, keys_differ


def budget_fails_closed() -> str:
    """Run with a one-cent ceiling and return the error it raised."""
    world = World()
    tools = ToolRegistry(inject_idempotency_key=True).register_all(
        world.tools()
    )
    model = MeteredModel(DAMAGED_LAMP_SHADE, routed=False, cache=None)
    loop = budgeted_loop(
        model,
        tools,
        budget_cents=1,
        principal=Principal.of("CUST-8841", "orders:read", "refunds:write"),
    )
    try:
        loop.run(DAMAGED_LAMP_SHADE.goal, run_id="run_ch25_budget")
    except BudgetExceeded as exc:
        return str(exc)
    return ""


def main() -> int:
    failures: list[str] = []

    print("prices are ILLUSTRATIVE placeholders, per million tokens:")
    for name, price in ILLUSTRATIVE_PRICES.items():
        print(
            f"  {name:<14} in {price.input_cents_per_million:>4}c  "
            f"out {price.output_cents_per_million:>5}c  "
            f"cache read x{CACHE_READ_MULTIPLIER}"
        )
    print(
        f"  a failed run costs {HUMAN_HANDLING_CENTS}c of human handling "
        f"({HUMAN_HANDLING_SECONDS}s), also illustrative"
    )
    print(f"  task set: {len(SCENARIOS)} Northstar tickets, graded on state")

    reports = measure_all()
    by_label = {r.label: r for r in reports}
    report_cost(reports)
    report_outcome(reports)
    report_failures(reports)

    baseline = by_label["baseline"]
    cached = by_label["cached"]
    routed = by_label["routed"]

    print("\n=== the inversion the chapter exists to prevent ===")
    print(
        f"routed cost per call    : {routed.cents_per_call:.3f}c vs "
        f"{baseline.cents_per_call:.3f}c baseline "
        f"({_delta(routed.cents_per_call, baseline.cents_per_call)})"
    )
    print(
        f"routed cost per success : {routed.cents_per_success:.2f}c vs "
        f"{baseline.cents_per_success:.2f}c baseline "
        f"({_delta(routed.cents_per_success, baseline.cents_per_success)})"
    )
    print(
        f"verified success        : {routed.successes}/{len(routed.runs)} "
        f"routed vs {baseline.successes}/{len(baseline.runs)} baseline"
    )
    print("the invoice got cheaper and the work got more expensive.")

    if not routed.cents_per_call < baseline.cents_per_call:
        failures.append("routing did not reduce cost per call")
    if not routed.cents_per_success > baseline.cents_per_success:
        failures.append("routing did not raise cost per verified success")
    if cached.successes != baseline.successes:
        failures.append("caching changed the graded outcome; it must not")
    if not cached.model_cents < baseline.model_cents:
        failures.append("caching did not reduce model spend")
    if routed.escalations < 1:
        failures.append("the deterministic check never fired")

    print("\n=== prefix stability ===")
    stable_rate, unstable_rate = prefix_stability()
    print(f"six requests, stable prefix        : hits {stable_rate:.0%}")
    print(f"six requests, run id in the prompt : hits {unstable_rate:.0%}")
    if not stable_rate > unstable_rate or unstable_rate != 0.0:
        failures.append(
            f"an unstable prefix should never hit; got {unstable_rate:.0%}"
        )

    print("\n=== tenant scoping ===")
    hits, misses, keys_differ = tenant_isolation()
    print(f"identical prefix, two tenants : {hits} hit(s), {misses} miss(es)")
    print(f"keys differ                   : {keys_differ}")
    if hits != 0 or misses != 2 or not keys_differ:
        failures.append("a second tenant was served from the first's entry")

    print("\n=== budget fails closed ===")
    raised = budget_fails_closed()
    print(f"budget_cents=1 : {raised or 'nothing raised'}")
    if not raised:
        failures.append("a one-cent budget did not stop the run")

    print("\n--- what this proves ---")
    print("A cost optimisation can only be judged against graded outcomes.")
    print("Budgets, prefix-stable caching, and capability routing each")
    print("either reduce or increase cost per success depending on the")
    print("task, and which one happens is measurable in advance.")

    if failures:
        print("\nFAILED:")
        for line in failures:
            print(f"  - {line}")
        return 1
    return 0


def _delta(new: float, old: float) -> str:
    """Render a change as a signed percentage of the old value."""
    if old == 0:
        return "n/a"
    change = (new - old) / old
    return f"{change:+.0%}"


if __name__ == "__main__":
    sys.exit(main())
