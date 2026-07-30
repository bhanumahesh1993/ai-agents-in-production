"""The local stack, as assertions.

``tests/test_refund_once.py`` is the test the chapter is built around.
This file covers the stack around it: the Compose file, the MCP boundary,
the four model modes, the cassette policy, and the fault catalogue.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from datetime import date

import cassettes
import faults
import pytest
import stack
from local_model import PROMOTION_CHECKS, local_provider, unmet
from mcp_server import (
    PROTOCOL_REVISION,
    SUPPORT_PRINCIPAL,
    GatewayRegistry,
    MCPServer,
    registry_for,
    serve_stdio,
)
from model_mode import MODES, load_cassette, load_script, mode_from_env
from northstar_contracts import ToolCall, World
from run_local import AMOUNT, ORDER, run_task

SCRIPT_DIR = Path(__file__).resolve().parent / "scripts"


# ----------------------------------------------------------------- the stack


def test_the_compose_file_declares_all_nine_services() -> None:
    """A service that vanished is a component the stack stopped modelling."""
    document = stack.load_compose()
    assert set(stack.REQUIRED_SERVICES) <= set(document["services"])
    assert len(stack.REQUIRED_SERVICES) == 9


def test_the_stack_has_no_dangling_dependency_or_floating_tag() -> None:
    """Validated by parsing, which is weaker than applying and not token."""
    assert stack.problems() == []
    assert stack.unpinned_images(stack.load_env()) == []


def test_every_image_variable_is_set_in_the_env_example() -> None:
    document = stack.load_compose()
    env = stack.load_env()
    for service, variable in stack.image_variables(document).items():
        assert variable in env, f"{service} uses an unset {variable}"


def test_the_agent_defaults_to_mock_mode_in_the_compose_file() -> None:
    """No credentials are read at any point, and the file has to say so."""
    agent = stack.load_compose()["services"]["agent"]
    assert agent["environment"]["MODEL_MODE"] == "mock"


def test_the_loader_refuses_what_it_does_not_understand() -> None:
    """A loader that guesses is a loader that will guess wrong about a port."""
    with pytest.raises(stack.ComposeError):
        stack.loads("services:\n\tagent: {}\n")
    with pytest.raises(stack.ComposeError):
        stack.loads("services:\n  agent: &anchor\n")


# ------------------------------------------------------------ the MCP gateway


def test_the_gateway_advertises_the_six_northstar_tools() -> None:
    server = MCPServer(World())
    reply = server.handle(
        {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
    )
    names = {tool["name"] for tool in reply["result"]["tools"]}
    assert names == {
        "get_order",
        "get_policy",
        "search_orders",
        "issue_refund",
        "send_message",
        "escalate_to_specialist",
    }


def test_the_gateway_reports_the_protocol_revision_it_implements() -> None:
    server = MCPServer(World())
    reply = server.handle(
        {"jsonrpc": "2.0", "id": 1, "method": "initialize"}
    )
    assert reply["result"]["protocolVersion"] == PROTOCOL_REVISION
    assert reply["result"]["capabilities"]["tools"] is not None


def test_policy_is_evaluated_at_the_gateway_before_the_tool_runs() -> None:
    """The reason the gateway exists locally at all."""
    world = World()
    server = MCPServer(
        world, principal=SUPPORT_PRINCIPAL.__class__(user_id="CUST-8841")
    )
    reply = server.handle(
        {
            "jsonrpc": "2.0",
            "id": "c1",
            "method": "tools/call",
            "params": {
                "id": "c1",
                "name": "issue_refund",
                "arguments": {
                    "order_id": ORDER,
                    "amount_cents": AMOUNT,
                    "reason": "damaged",
                },
            },
        }
    )
    # No scopes, so the refund never reaches the world.
    assert "error" in reply
    assert world.ledger == []
    assert server.calls[-1]["decision"] == "deny"


def test_an_authorization_refusal_reaches_the_model_as_permanent() -> None:
    """A retryable denial sends the agent round the loop against a wall."""
    world = World()
    server = MCPServer(
        world, principal=SUPPORT_PRINCIPAL.__class__(user_id="CUST-8841")
    )
    registry = GatewayRegistry(server)
    result = registry.dispatch(
        ToolCall("c1", "issue_refund",
                 {"order_id": ORDER, "amount_cents": AMOUNT,
                  "reason": "damaged"}),
        run_id="r",
        step=0,
    )
    assert result.ok is False
    assert result.retryable is False


def test_an_unknown_tool_is_refused_by_the_gateway() -> None:
    server = MCPServer(World())
    reply = server.handle(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "wire_transfer", "arguments": {}},
        }
    )
    assert "error" in reply


def test_the_registry_cannot_be_bypassed() -> None:
    """If a tool ran locally, the enforcement point was not enforcing."""
    registry = registry_for(World())
    spec, fn = registry.bindings()[0]
    with pytest.raises(RuntimeError):
        fn()


def test_the_gateway_serves_json_rpc_over_stdio() -> None:
    """Same server, real transport, still no network."""
    import io

    stdin = io.StringIO(
        '{"jsonrpc": "2.0", "id": 1, "method": "tools/list"}\n'
        '{"jsonrpc": "2.0", "id": 2, "method": "nope"}\n'
    )
    stdout = io.StringIO()
    assert serve_stdio(stdin, stdout, World()) == 0
    lines = [line for line in stdout.getvalue().splitlines() if line]
    assert len(lines) == 2
    assert '"result"' in lines[0]
    assert '"error"' in lines[1]


# ------------------------------------------------------------- model modes


def test_mock_is_the_default_and_a_typo_fails_closed() -> None:
    """The default is the decision that matters most."""
    assert mode_from_env({}) == "mock"
    assert mode_from_env({"MODEL_MODE": "live"}) == "live"
    with pytest.raises(ValueError):
        mode_from_env({"MODEL_MODE": "liv"})


def test_there_are_four_modes_not_two() -> None:
    assert MODES == ("mock", "replay", "record", "live")


def test_replay_is_the_same_class_with_a_different_source() -> None:
    """One code path for both, which is why there is no ReplayModel."""
    hand_written = load_script("refund.json", SCRIPT_DIR)
    recorded = load_cassette("refund.jsonl", SCRIPT_DIR)
    assert len(hand_written) == len(recorded)
    assert isinstance(hand_written[-1], str)
    assert isinstance(recorded[-1], str)


def test_mock_and_replay_leave_the_same_world() -> None:
    mock = run_task(mode="mock", inject="timeout")
    replay = run_task(mode="replay", inject="timeout")
    assert mock.refund_rows == replay.refund_rows == 1
    assert mock.refunded_cents == replay.refunded_cents == AMOUNT


# --------------------------------------------------------------- cassettes


def test_a_cassette_carries_its_provenance() -> None:
    cassette = cassettes.load(SCRIPT_DIR / "refund.jsonl")
    assert cassette.model
    assert cassette.provider
    assert cassette.recorded_at
    assert cassette.records == 5


def test_a_cassette_ages_out() -> None:
    """Passing unchanged for eight months is not evidence of health."""
    cassette = cassettes.load(SCRIPT_DIR / "refund.jsonl")
    assert cassette.is_expired(date(2026, 8, 1)) is False
    assert cassette.is_expired(date(2027, 1, 1)) is True


def test_an_unstamped_cassette_counts_as_expired() -> None:
    """Failing closed on missing provenance is what gives the check teeth."""
    unstamped = cassettes.Cassette(
        Path("x.jsonl"), "m", "p", recorded_at="", records=1
    )
    assert unstamped.age_days() is None
    assert unstamped.is_expired() is True


def test_the_shipped_cassette_is_redacted() -> None:
    """Redact on write. Redacting on read leaves the bytes in every clone."""
    assert cassettes.unredacted(SCRIPT_DIR / "refund.jsonl") == []


def test_the_redactor_masks_a_recorded_request() -> None:
    redacted = cassettes.redactor().redact(
        {"request": [{"role": "user", "content": "ada@example.com"}],
         "api_key": "sk-secret"}
    )
    assert "sk-secret" not in str(redacted)
    assert "ada@example.com" not in str(redacted)


# -------------------------------------------------------------- the faults


def test_every_catalogued_fault_has_a_distinct_correct_response() -> None:
    """One generic "make it fail" switch teaches the wrong lesson."""
    responses = [f.response for f in faults.catalogue()]
    assert len(responses) == len(faults.FAULTS)
    assert len(set(responses)) == len(responses)


def test_a_fault_the_world_cannot_produce_says_so() -> None:
    """Silently substituting a different failure would fake the coverage."""
    world = World()
    with pytest.raises(NotImplementedError):
        faults.apply(world, "issue_refund", "expired_token")
    assert faults.unsupported() == ["expired_token", "partial"]


def test_injecting_the_timeout_is_deterministic() -> None:
    """The failure happens on every run, not one run in a hundred."""
    for _ in range(3):
        run = run_task(mode="mock", inject="timeout")
        assert run.tool_attempts("issue_refund") == 3
        assert run.refund_rows == 1


def test_a_clean_run_needs_no_retry() -> None:
    run = run_task(mode="mock", inject=None)
    assert run.refund_rows == 1
    assert run.refunded_cents == AMOUNT


# ---------------------------------------------------------- local inference


def test_a_promotion_check_nobody_ran_does_not_count_as_passing() -> None:
    assert unmet({}) == list(PROMOTION_CHECKS)
    assert len(unmet(dict.fromkeys(PROMOTION_CHECKS, True))) == 0


def test_local_inference_never_falls_back_to_a_hosted_endpoint() -> None:
    """It names the variable instead, which is the safe direction."""
    from northstar_runtime import LiveModelUnavailable

    with pytest.raises(LiveModelUnavailable):
        local_provider({})
