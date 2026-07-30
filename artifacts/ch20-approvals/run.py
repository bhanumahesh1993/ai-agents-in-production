"""The 24,000-cent refund, wired end to end so it can be paused.

One trajectory, one world, one inbox, and three endings the demo drives:
approve and resume, approve then tamper with the amount before resume, and
let the approval expire.

The tamper is not a contrived edit. It is the June incident: the run was
parked overnight, the customer sent a second message, and on resume the
agent re-planned and asked for a different number. Nothing about that is
adversarial, and the control has to hold anyway.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any

from northstar_contracts import (
    Message,
    RunState,
    ToolCall,
    ToolResult,
    World,
    idempotency_key,
)
from northstar_policy import Principal
from northstar_runtime import (
    AgentLoop,
    FakeModel,
    SqliteCheckpointer,
    ToolRegistry,
)

from budget import BudgetGuard
from classes import northstar_policy_bundle
from fingerprint import ToolVersions
from guard import Guard
from inbox import TaskInbox

__all__ = [
    "AMOUNT",
    "LAMP_SHADE_CENTS",
    "ORDER",
    "PRINCIPAL",
    "RUN_ID",
    "SMALL_ORDER",
    "TAMPERED_AMOUNT",
    "TOOL_VERSION",
    "ApprovalRun",
    "RecordingTools",
    "close_open_checkpoints",
    "refund_call",
    "replan",
    "script",
    "start_run",
]

#: The flagged order from the chapter's opening. 24,000 cents, two field
#: speakers, `fraud_review`. Both the flag and the amount route it to a
#: human, which is deliberate: a control with one trigger has one bug away
#: from having none.
ORDER = "NR-2026-0042110"
AMOUNT = 24000

#: What the resumed run asked for after re-planning. Ten times the amount,
#: one changed integer.
TAMPERED_AMOUNT = 240000

#: The under-threshold case, for the automatic class.
SMALL_ORDER = "NR-2026-0041827"
LAMP_SHADE_CENTS = 3250

RUN_ID = "run_ch20_refund"
GOAL = "Customer reports two damaged field speakers on this order."

#: Northstar's refund tool is on version 3 in this chapter. Bumping it is
#: how the demo invalidates a parked approval.
TOOL_VERSION = "3"

#: Every SQLite checkpointer this module opens. A ``:memory:`` connection
#: that is garbage collected without being closed raises a
#: ``ResourceWarning``, and this repository runs pytest with warnings as
#: errors, which is the correct setting and the reason this list exists.
OPEN_CHECKPOINTS: list[SqliteCheckpointer] = []

PRINCIPAL = Principal(
    user_id="CUST-9032",
    agent_id="northstar-support-agent",
    operator_id="northstar-platform",
    scopes=frozenset({"orders.read", "refunds.write"}),
)


def refund_call(
    amount_cents: int = AMOUNT,
    order_id: str = ORDER,
    call_id: str = "c3",
) -> ToolCall:
    """The refund the agent proposes, with its derived idempotency key.

    Identity decides whether the write is permitted, the approval decides
    whether a human agreed to this exact one, and the key decides what a
    second attempt at a permitted, approved write does. Three independent
    mechanisms, and a production refund path needs all three.
    """
    return ToolCall(
        call_id,
        "issue_refund",
        {
            "order_id": order_id,
            "amount_cents": amount_cents,
            "reason": "damaged",
            "idempotency_key": idempotency_key(RUN_ID, "refund"),
        },
    )


def script(amount_cents: int = AMOUNT) -> list[Any]:
    """Read the order, read the policy, refund, reply."""
    return [
        ToolCall("c1", "get_order", {"order_id": ORDER}),
        ToolCall("c2", "get_policy", {"reason": "damaged"}),
        refund_call(amount_cents),
        f"Refunded {amount_cents} cents for the damaged speakers.",
    ]


class RecordingTools(ToolRegistry):
    """The registry the loop dispatches through.

    Its one addition is a line after the effect: it hands the landed result
    to the guard, which is where evidence for the next approval payload
    comes from and where a committed write is counted against the run's
    caps. Both have to happen *after* the effect, and neither belongs
    inside a tool.
    """

    def __init__(self, holder: list[Any]) -> None:
        super().__init__(inject_idempotency_key=False)
        self._holder = holder

    def dispatch(
        self,
        call: ToolCall,
        run_id: str | None = None,
        step: int | None = None,
    ) -> ToolResult:
        """Run the call, then tell the guard what landed."""
        result = super().dispatch(call, run_id=run_id, step=step)
        guard = self._holder[0] if self._holder else None
        if guard is not None:
            guard.note(call, result, self.spec_for(call.name))
        return result


@dataclass
class ApprovalRun:
    """Everything the demo and the tests look at afterwards."""

    world: World
    loop: AgentLoop
    guard: Guard
    inbox: TaskInbox
    state: RunState
    checkpointer: SqliteCheckpointer | None = None

    def close(self) -> None:
        """Release the checkpoint database."""
        if self.checkpointer is not None:
            self.checkpointer.close()
            self.checkpointer = None

    @property
    def refunded_cents(self) -> int:
        """What landed in the world, not what the agent said."""
        return self.world.total_refunded_cents(ORDER)

    @property
    def refund_rows(self) -> int:
        """How many refund rows exist. Two of 12,000 is not one of 24,000."""
        return len(self.world.refunds_for(ORDER))

    @property
    def pending(self) -> list[Any]:
        """The human's queue."""
        return self.inbox.pending()

    def resume(self, state: RunState | None = None) -> RunState:
        """Drive the run forward from a state, and remember where it got."""
        self.state = self.loop.resume(state if state is not None else
                                      self.state)
        return self.state


def start_run(
    *,
    amount_cents: int = AMOUNT,
    inbox_path: str | None = None,
    clock: Any = None,
    max_writes: int = 3,
) -> ApprovalRun:
    """Build the whole path and run it until it needs a human.

    Returns:
        An :class:`ApprovalRun` parked in ``waiting_approval``, unless the
        amount is below the threshold and the order is not flagged, in
        which case it has already finished.
    """
    world = World()
    holder: list[Any] = [None]
    tools = RecordingTools(holder)
    tools.register_all(world.tools())
    versions = ToolVersions(
        tools.specs(), overrides={"issue_refund": TOOL_VERSION}
    )
    inbox = TaskInbox(
        PRINCIPAL, versions, path=inbox_path, clock=clock
    )
    guard = Guard(
        policy=northstar_policy_bundle(),
        principal=PRINCIPAL,
        inbox=inbox,
        budget=BudgetGuard(max_writes=max_writes),
        tools=tools,
        tool_versions=versions,
        world=world,
        clock=clock or (lambda: 0.0),
    )
    holder[0] = guard
    checkpointer = SqliteCheckpointer(":memory:")
    OPEN_CHECKPOINTS.append(checkpointer)
    loop = AgentLoop(
        model=FakeModel(default=script(amount_cents)),
        tools=tools,
        checkpointer=checkpointer,
        policy=guard,
        principal=PRINCIPAL,
        approvals=inbox,
        max_turns=8,
    )
    state = loop.run(GOAL, run_id=RUN_ID)
    return ApprovalRun(
        world=world,
        loop=loop,
        guard=guard,
        inbox=inbox,
        state=state,
        checkpointer=checkpointer,
    )


def close_open_checkpoints() -> int:
    """Close every checkpoint database this module opened.

    Returns:
        How many were closed.
    """
    count = len(OPEN_CHECKPOINTS)
    for checkpointer in OPEN_CHECKPOINTS:
        checkpointer.close()
    OPEN_CHECKPOINTS.clear()
    return count


def replan(state: RunState, amount_cents: int = TAMPERED_AMOUNT) -> RunState:
    """Rewrite the parked call's amount, and hand back the new state.

    The suspended run's decision is already an assistant message holding a
    ``tool_use`` block. On resume the loop re-authorises *those exact
    calls* rather than asking the model again, so editing the block here is
    the faithful way to say "the agent decided differently while it waited".

    The demo calls this twice, and the difference between the two calls is
    the whole chapter. When the *agent* re-plans, no decision matches the
    new fingerprint, so the run re-requests and the ledger stays empty.
    When the *approver* corrects the call to 8,400 cents, a decision for
    exactly that fingerprint already exists, so the identical edit
    proceeds. The mechanism does not care who is honest. It cares which
    call a human actually agreed to.
    """
    if not state.messages:
        return state
    last = state.messages[-1]
    if last.role != "assistant" or not isinstance(last.content, list):
        return state
    blocks: list[Any] = []
    for block in last.content:
        if (
            isinstance(block, dict)
            and block.get("type") == "tool_use"
            and block.get("name") == "issue_refund"
        ):
            blocks.append(
                {
                    **block,
                    "input": {
                        **block["input"],
                        "amount_cents": amount_cents,
                    },
                }
            )
        else:
            blocks.append(block)
    edited = Message(role="assistant", content=blocks)
    return replace(state, messages=[*state.messages[:-1], edited])
