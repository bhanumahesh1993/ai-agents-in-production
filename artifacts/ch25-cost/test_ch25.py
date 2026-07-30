"""The Chapter 25 cost properties, as assertions.

Each test asserts on a measured quantity — a graded outcome, a token
count, a hit, a raised limit — rather than on a printed string.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import pytest  # noqa: E402
from budgets import (  # noqa: E402
    HUMAN_HANDLING_CENTS,
    LARGE_MODEL,
    SMALL_MODEL,
    budgeted_loop,
)
from cache import PrefixCache, assemble, cache_key  # noqa: E402
from compare import (  # noqa: E402
    Config,
    MeteredModel,
    measure,
    measure_all,
    run_scenario,
)
from northstar_contracts import Message, ToolResult, World  # noqa: E402
from northstar_policy import BudgetExceeded, Principal  # noqa: E402
from northstar_runtime import ToolRegistry  # noqa: E402
from northstar_telemetry.cost import NANOCENTS  # noqa: E402
from router import CHEAP_STEPS, escalate, route  # noqa: E402
from scenarios import (  # noqa: E402
    CHANGED_MIND_MUG,
    DAMAGED_LAMP_SHADE,
    MUG_CHANGED_MIND_CENTS,
    MUG_ORDER,
    OVER_REFUND_MUG,
)

ROUTED = Config("routed", cached=False, routed=True)
BASELINE = Config("baseline", cached=False, routed=False)


def test_routing_cuts_cost_per_call_and_raises_cost_per_success() -> None:
    """The inversion the chapter exists to prevent, measured."""
    reports = {r.label: r for r in measure_all()}
    baseline = reports["baseline"]
    routed = reports["routed"]

    # The invoice line falls: fewer expensive tokens.
    assert routed.cents_per_call < baseline.cents_per_call
    # The number that decides rises, because a graded success got rarer.
    assert routed.cents_per_success > baseline.cents_per_success
    assert routed.successes < baseline.successes
    assert baseline.verified_success_rate == 1.0


def test_the_cheap_model_fails_in_a_way_no_deterministic_check_sees() -> None:
    """Well formed, reconciles against the ledger, and 1625c too much."""
    result = run_scenario(CHANGED_MIND_MUG, ROUTED)

    assert result.escalations == 0, "the check should not have fired"
    assert not result.passed
    # The world took the wrong refund, and only the grader knows it.
    assert result.grade.details["world"]["refund_count"] == 1
    correct = run_scenario(CHANGED_MIND_MUG, BASELINE)
    assert correct.passed
    assert MUG_CHANGED_MIND_CENTS == 1625


def test_escalation_redoes_a_step_that_changed_nothing() -> None:
    """A refused call leaves the world untouched, so a rerun is safe."""
    routed = run_scenario(OVER_REFUND_MUG, ROUTED)

    assert routed.escalations == 1
    assert routed.passed
    # The rerun went to the large model, and only one refund landed.
    assert routed.calls[-2].model == LARGE_MODEL
    assert routed.grade.details["world"]["refund_count"] == 1
    assert routed.grade.details["world"]["orders"][MUG_ORDER][
        "refunded_cents"
    ] == 3250


def test_escalation_is_deterministic_not_a_second_opinion() -> None:
    """``escalate`` reads schema and arithmetic, never a model."""
    world = World()
    malformed = ToolResult("c1", ok=True, content={"refund_id": "RFND-1"})
    refused = ToolResult.failure("c2", "over the order value", retryable=False)
    plain_read = ToolResult("c3", ok=True, content={"order_id": "NR-1"})

    assert escalate(malformed, world)
    assert escalate(refused, world)
    assert not escalate(plain_read, world)


def test_cache_key_is_scoped_by_tenant_before_content() -> None:
    """No key a second tenant can compute collides with the first."""
    prefix = [Message(role="system", content="identical prompt")]
    assert cache_key("acme", prefix) != cache_key("globex", prefix)

    cache = PrefixCache()
    assert cache.lookup("acme", prefix) == 0        # cold
    assert cache.lookup("acme", prefix) > 0         # warm
    assert cache.lookup("globex", prefix) == 0      # cold again, on purpose
    assert cache.misses == 2
    assert cache.entries == 2


def test_an_unstable_prefix_never_gets_a_hit() -> None:
    """A run id at the front of the prompt turns the discount off."""
    specs = World().tool_specs()
    conversation = [Message(role="user", content="where is my order")]

    stable = PrefixCache()
    unstable = PrefixCache()
    for index in range(5):
        stable.lookup(
            "acme", assemble("system", specs, conversation).prefix
        )
        unstable.lookup(
            "acme",
            assemble(
                "system", specs, conversation, run_marker=f"run-{index}"
            ).prefix,
        )

    assert stable.hits == 4
    assert unstable.hits == 0
    assert stable.hit_rate > unstable.hit_rate


def test_caching_changes_the_bill_and_not_the_outcome() -> None:
    """Same graded results, fewer billed input tokens."""
    plain = measure(Config("plain", cached=False, routed=False))
    warm = measure(Config("warm", cached=True, routed=False))

    assert warm.successes == plain.successes
    assert warm.cached_tokens > 0
    assert plain.cached_tokens == 0
    assert warm.model_nanocents < plain.model_nanocents
    assert warm.p95_ms < plain.p95_ms


def test_every_reported_aggregate_is_the_sum_of_the_runs() -> None:
    """The report computes its statistics; it does not assert them."""
    report = measure(BASELINE)

    assert report.input_tokens == sum(
        c.input_tokens for r in report.runs for c in r.calls
    )
    assert report.model_calls == sum(len(r.calls) for r in report.runs)
    assert report.human_cents == (
        len(report.runs) - report.successes
    ) * HUMAN_HANDLING_CENTS
    per_call = report.model_nanocents / NANOCENTS / report.model_calls
    assert report.cents_per_call == pytest.approx(per_call)


def test_route_sends_planning_to_the_large_model() -> None:
    """The classification is the routing decision, and it is explicit."""
    assert route("plan") == "large"
    assert route("reply") == "large"
    assert route("triage") == "small"
    assert "triage" in CHEAP_STEPS and "plan" not in CHEAP_STEPS


def test_a_one_cent_budget_stops_the_run() -> None:
    """Every limit raises. The model does not get a vote."""
    world = World()
    tools = ToolRegistry(inject_idempotency_key=True).register_all(
        world.tools()
    )
    loop = budgeted_loop(
        MeteredModel(DAMAGED_LAMP_SHADE, routed=False, cache=None),
        tools,
        budget_cents=1,
        principal=Principal.of("CUST-8841", "orders:read", "refunds:write"),
    )
    with pytest.raises(BudgetExceeded) as caught:
        loop.run(DAMAGED_LAMP_SHADE.goal, run_id="run_ch25_test_budget")

    assert caught.value.kind == "cents"
    assert world.total_refunded_cents("NR-2026-0041827") == 0


def test_the_routed_run_used_both_model_classes() -> None:
    """Routing is real: the receipts name two different models."""
    result = run_scenario(DAMAGED_LAMP_SHADE, ROUTED)
    models = {c.model for c in result.calls}

    assert models == {SMALL_MODEL, LARGE_MODEL}
    assert result.passed, "routing the read step is safe on this ticket"
