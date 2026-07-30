"""The test nobody at Northstar could write.

Nine lines of setup and one assertion. It runs in single-digit
milliseconds, needs no credentials, fails loudly against the
non-idempotent refund tool from Chapter 1, and passes against the repaired
one. The third and fourth ``ToolCall``s are the point: the retry is not
hoped for, it is stated. The model is no longer the system under test. The
refund path is.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from northstar_contracts import ToolCall, World, idempotency_key
from northstar_runtime import AgentLoop, FakeModel

from mcp_server import SUPPORT_PRINCIPAL, registry_for

ORDER = "NR-2026-0041827"
RUN_ID = "run_ch21_local"
PAY = {
    "order_id": ORDER,
    "amount_cents": 3250,
    "reason": "damaged",
    # Derived from (run_id, step_id), never generated. The same key on the
    # first attempt and on the retry is what collapses the second effect.
    "idempotency_key": idempotency_key(RUN_ID, "refund"),
}


def test_timeout_retry_refunds_once() -> None:
    """A timeout is not a failure, and a stated retry proves the repair."""
    world = World()
    world.inject_fault("issue_refund", kind="timeout")
    model = FakeModel(default=[
        ToolCall(id="1", name="get_order",
                 arguments={"order_id": ORDER}),
        ToolCall(id="2", name="issue_refund", arguments=PAY),
        ToolCall(id="3", name="issue_refund", arguments=PAY),
        "Refunded the cracked lamp shade.",
    ])
    loop = AgentLoop(model, registry_for(world), max_turns=8,
                     principal=SUPPORT_PRINCIPAL)
    loop.run("Customer reports a cracked lamp shade.", run_id=RUN_ID)

    assert [r.amount_cents for r in world.refunds_for(ORDER)] == [3250]


def test_the_same_run_without_a_key_pays_twice() -> None:
    """The negative control. Without it the test above proves nothing.

    This is Chapter 1's incident reproduced on the local stack, and it is
    the reason the assertion above is worth writing: the harness, the
    gateway, the policy, and the world are identical, and only the key
    differs.
    """
    world = World()
    world.inject_fault("issue_refund", kind="timeout")
    unkeyed = {k: v for k, v in PAY.items() if k != "idempotency_key"}
    model = FakeModel(default=[
        ToolCall(id="1", name="get_order", arguments={"order_id": ORDER}),
        ToolCall(id="2", name="issue_refund", arguments=unkeyed),
        ToolCall(id="3", name="issue_refund", arguments=unkeyed),
        "Refunded the cracked lamp shade.",
    ])
    loop = AgentLoop(model, registry_for(world), max_turns=8,
                     principal=SUPPORT_PRINCIPAL)
    state = loop.run("Customer reports a cracked lamp shade.", run_id=RUN_ID)

    # The run is happy. The world is not.
    assert state.status == "succeeded"
    assert [r.amount_cents for r in world.refunds_for(ORDER)] == [3250, 3250]


def test_a_deterministic_model_makes_the_trajectory_an_input() -> None:
    """The same script, twice, produces the same world."""
    ledgers = []
    for _ in range(2):
        world = World()
        world.inject_fault("issue_refund", kind="timeout")
        model = FakeModel(default=[
            ToolCall(id="1", name="get_order",
                     arguments={"order_id": ORDER}),
            ToolCall(id="2", name="issue_refund", arguments=PAY),
            ToolCall(id="3", name="issue_refund", arguments=PAY),
            "Refunded the cracked lamp shade.",
        ])
        loop = AgentLoop(model, registry_for(world), max_turns=8,
                         principal=SUPPORT_PRINCIPAL)
        loop.run("Customer reports a cracked lamp shade.", run_id=RUN_ID)
        ledgers.append(world.snapshot())

    assert ledgers[0] == ledgers[1]
