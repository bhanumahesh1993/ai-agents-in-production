"""The loop: dispatch, failure handling, budgets, cancellation, retries."""

from __future__ import annotations

import pytest
from northstar_contracts import ToolCall, World
from northstar_policy import BudgetExceeded, TurnLimitExceeded
from northstar_runtime import (
    AgentLoop,
    FakeModel,
    FlakyModel,
    LiveModel,
    LiveModelUnavailable,
    RunCancelled,
    ScriptExhausted,
    ToolRegistry,
    truncate_to_budget,
)
from northstar_runtime.providers import ModelResponse

from conftest import DAMAGED_ORDER, DELIVERED_ORDER, refund_script


def test_happy_path_reaches_the_right_world_state(world: World) -> None:
    """The baseline the rest of the suite is measured against."""
    loop = AgentLoop(
        model=FakeModel(default=refund_script()), tools=world.tools()
    )
    state = loop.run("refund the damaged mug", run_id="run-1")

    assert state.status == "succeeded"
    assert state.step == 4
    assert world.total_refunded_cents(DAMAGED_ORDER) == 3250
    assert state.final_text is not None
    assert "32.50" in state.final_text


def test_tool_failure_is_an_observation_not_an_exception(
    world: World,
) -> None:
    """A failing tool never breaks the loop. The model gets to react."""
    loop = AgentLoop(
        model=FakeModel(
            default=[
                ToolCall("c1", "get_order", {"order_id": "NR-0000-0000000"}),
                "I could not find that order. Could you check the number?",
            ]
        ),
        tools=world.tools(),
    )
    state = loop.run("look up a bad order id", run_id="run-1")

    assert state.status == "succeeded"
    observation = state.messages[3].content
    assert observation["ok"] is False
    assert "no order" in observation["content"]["error"]


def test_unknown_tool_gets_an_error_that_helps_the_model(
    world: World,
) -> None:
    """An error message is prompt text. Make it list the real options."""
    loop = AgentLoop(
        model=FakeModel(
            default=[
                ToolCall("c1", "refund_everything", {}),
                "Sorry, I cannot do that.",
            ]
        ),
        tools=world.tools(),
    )
    state = loop.run("do the thing", run_id="run-1")
    error = state.messages[3].content["content"]["error"]

    assert "no tool named 'refund_everything'" in error
    assert "issue_refund" in error


def test_bad_arguments_are_caught_before_the_tool_runs(
    world: World,
) -> None:
    """Validation lives at the boundary, not in every tool."""
    loop = AgentLoop(
        model=FakeModel(
            default=[
                ToolCall(
                    "c1",
                    "issue_refund",
                    {"order_id": DAMAGED_ORDER, "reason": "damaged"},
                ),
                "I need the amount.",
            ]
        ),
        tools=world.tools(),
    )
    state = loop.run("refund", run_id="run-1")
    error = state.messages[3].content["content"]["error"]

    assert "missing required argument" in error
    assert "amount_cents" in error
    assert world.call_count("issue_refund") == 0


def test_turn_limit_raises(world: World) -> None:
    """A loop that never terminates is stopped by the harness."""
    looping = FakeModel(
        default=[ToolCall("", "get_policy", {})] * 20, strict=False
    )
    loop = AgentLoop(model=looping, tools=world.tools(), max_turns=3)

    with pytest.raises(TurnLimitExceeded) as exc:
        loop.run("loop forever", run_id="run-1")

    assert exc.value.kind == "turns"
    assert exc.value.run_id == "run-1"


def test_budget_exhaustion_raises_and_records_the_spend(
    world: World,
) -> None:
    """The money is gone whether or not the run survives. Record it."""
    loop = AgentLoop(
        model=FakeModel(default=refund_script()),
        tools=world.tools(),
        budget_cents=2,
        cost_fn=lambda response: 3,
    )

    with pytest.raises(BudgetExceeded) as exc:
        loop.run("refund the mug", run_id="run-1")

    assert exc.value.kind == "cents"
    finished = loop.events.of_type("run.finished")
    assert finished[-1]["payload"]["status"] == "failed"


def test_budget_stops_the_run_before_the_write(world: World) -> None:
    """Budget checks happen before side effects, not after them."""
    loop = AgentLoop(
        model=FakeModel(
            default=[
                ToolCall(
                    "c1",
                    "issue_refund",
                    {
                        "order_id": DAMAGED_ORDER,
                        "amount_cents": 3250,
                        "reason": "damaged",
                    },
                ),
                "Done.",
            ]
        ),
        tools=world.tools(),
        budget_cents=0,
        cost_fn=lambda response: 5,
    )

    with pytest.raises(BudgetExceeded):
        loop.run("refund", run_id="run-1")

    assert world.total_refunded_cents(DAMAGED_ORDER) == 0


def test_cancellation_lands_between_steps(world: World) -> None:
    """A kill switch stops the loop cleanly, never mid-write."""
    loop = AgentLoop(
        model=FakeModel(default=refund_script()), tools=world.tools()
    )
    loop.step_hook = lambda state: loop.cancel("operator pulled the switch")

    with pytest.raises(RunCancelled) as exc:
        loop.run("refund the mug", run_id="run-1")

    assert exc.value.run_id == "run-1"
    assert world.total_refunded_cents(DAMAGED_ORDER) == 0


def test_parallel_tool_calls_in_one_turn(world: World) -> None:
    """A turn may request several tools; all of them are observed."""
    loop = AgentLoop(
        model=FakeModel(
            default=[
                [
                    ToolCall(
                        "c1", "get_order", {"order_id": DELIVERED_ORDER}
                    ),
                    ToolCall("c2", "get_policy", {"reason": "damaged"}),
                ],
                "Both read.",
            ]
        ),
        tools=world.tools(),
    )
    state = loop.run("read both", run_id="run-1")

    tool_messages = [m for m in state.messages if m.role == "tool"]
    assert len(tool_messages) == 2
    assert state.status == "succeeded"


def test_result_truncation_keeps_the_shape(world: World) -> None:
    """Truncation drops rows and says so; it does not corrupt the payload."""
    content = {"results": [{"order_id": f"NR-{i:07d}"} for i in range(50)]}
    shrunk, truncated = truncate_to_budget(content, 40)

    assert truncated
    assert shrunk["truncated"] is True
    assert shrunk["omitted_items"] > 0
    assert len(shrunk["results"]) < 50


def test_events_tell_the_whole_story(world: World) -> None:
    """Every decision the loop made is recoverable from the event log."""
    loop = AgentLoop(
        model=FakeModel(default=refund_script()), tools=world.tools()
    )
    loop.run("refund the mug", run_id="run-1")
    types = [event["type"] for event in loop.events.records]

    assert types[0] == "run.started"
    assert types[-1] == "run.finished"
    assert types.count("model.called") == 4
    assert types.count("tool.called") == 3
    assert types.count("tool.result") == 3


def test_fake_model_is_a_pure_function_of_the_conversation(
    world: World,
) -> None:
    """Two loops sharing one model instance do not interfere."""
    model = FakeModel(default=refund_script())
    first = AgentLoop(model=model, tools=World().tools()).run(
        "refund the mug", run_id="run-1"
    )
    second = AgentLoop(model=model, tools=World().tools()).run(
        "refund the mug", run_id="run-2"
    )

    assert first.final_text == second.final_text
    assert first.step == second.step


def test_script_exhaustion_is_loud(world: World) -> None:
    """Running off the end of a script means the agent surprised you."""
    loop = AgentLoop(
        model=FakeModel(
            default=[ToolCall("c1", "get_policy", {})]
        ),
        tools=world.tools(),
    )

    with pytest.raises(ScriptExhausted):
        loop.run("read the policy", run_id="run-1")


def test_flaky_model_is_reproducible(world: World) -> None:
    """Seeded flakiness. A failure you cannot reproduce is not a test."""

    def run_once() -> tuple[str, int]:
        w = World()
        flaky = FlakyModel(
            FakeModel(default=refund_script()),
            seed=7,
            p_repeat=0.3,
            p_stall=0.2,
        )
        state = AgentLoop(
            model=flaky, tools=w.tools(), max_turns=10
        ).run("refund the damaged mug", run_id="run-1")
        return state.status, w.total_refunded_cents(DAMAGED_ORDER)

    assert run_once() == run_once()


def test_live_model_is_opt_in_and_explains_itself() -> None:
    """Mock mode must not need a key, and the live error must name the fix.

    Whichever is missing first — the SDK or the key — the message says
    which command to run. A repository whose suite fails on a machine
    without credentials has broken its own mock-mode promise.
    """
    model = LiveModel(provider="anthropic", api_key_env="NORTHSTAR_ABSENT")

    with pytest.raises(LiveModelUnavailable, match="pip install|export"):
        model.complete([], [])


def test_model_response_round_trips() -> None:
    """The journal stores responses as dicts; they must come back whole."""
    response = ModelResponse(
        text="hello",
        tool_calls=[ToolCall("c1", "get_order", {"order_id": "NR-1"})],
        input_tokens=10,
        output_tokens=4,
        model="fake-model-1",
        stop_reason="tool_use",
    )

    assert ModelResponse.from_dict(response.to_dict()) == response


def test_registry_rejects_duplicate_names(world: World) -> None:
    """Two tools with one name is a coin flip at dispatch time."""
    registry = ToolRegistry().register_all(world.tools())
    spec = registry.specs()[0]

    with pytest.raises(ValueError, match="already registered"):
        registry.register(spec, world.get_order)
