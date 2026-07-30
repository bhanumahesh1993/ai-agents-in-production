"""Port two: a graph runtime, with nodes, edges, and a state schema.

The shape the LangGraph-style runtimes ask for, reduced to the parts that
change the answer. You declare the state that flows between nodes, you
declare the nodes, you declare the edges, and the runtime persists between
every node rather than between every turn.

That last sentence is the trade. The glue roughly triples and you buy a
checkpoint boundary *inside* the turn, so a worker killed after a write has
landed resumes knowing the call was attempted instead of replaying it.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field, replace
from typing import Any

import shared.triage
from northstar_contracts import (
    Message,
    Money,
    RunState,
    RunStatus,
    ToolCall,
    ToolSpec,
)
from northstar_policy import Decision, PolicyEngine, Principal
from northstar_runtime import (
    DEFAULT_SYSTEM_PROMPT,
    ModelProvider,
    SqliteCheckpointer,
    ToolRegistry,
    default_cost_cents,
)

__all__ = ["END", "GraphPort", "GraphState", "Node"]

#: Terminal node name. An edge function returning this stops the runtime.
END = "__end__"


@dataclass(frozen=True)
class GraphState:
    """The state schema. Everything that crosses a node boundary.

    A graph runtime cannot infer this for you: a node is a pure function
    from state to state, so anything a later node needs has to be a field
    here. Declaring it is most of the extra glue, and it is also the part
    that makes the checkpoint boundary meaningful.
    """

    run_id: str
    step: int = 0
    messages: list[Message] = field(default_factory=list)
    status: RunStatus = "running"
    budget_spent_cents: Money = 0
    #: Calls the agent node selected and the tool node has not run yet.
    #: This field is the reason the graph resumes mid-turn and the plain
    #: loop does not.
    pending: list[ToolCall] = field(default_factory=list)

    def as_run_state(self) -> RunState:
        """Project onto the portable ``RunState`` the port must return."""
        return RunState(
            run_id=self.run_id,
            step=self.step,
            messages=list(self.messages),
            status=self.status,
            budget_spent_cents=self.budget_spent_cents,
        )

    @classmethod
    def of(cls, state: RunState) -> GraphState:
        """Rebuild graph state from a checkpoint, pending calls included.

        The pending set is recovered from the transcript rather than from a
        side table: an assistant turn whose tool calls have no observation
        after them is a call the runtime selected and did not confirm. Keep
        it anywhere else and it does not survive the process that died.
        """
        return cls(
            run_id=state.run_id,
            step=state.step,
            messages=list(state.messages),
            status=state.status,
            budget_spent_cents=state.budget_spent_cents,
            pending=_unobserved_calls(state.messages),
        )


def _unobserved_calls(messages: list[Message]) -> list[ToolCall]:
    """Tool calls the model asked for that no observation answers."""
    if not messages or messages[-1].role != "assistant":
        return []
    return messages[-1].tool_calls


#: One node: a named pure function over the state schema.
Node = Callable[[GraphState], GraphState]


class GraphPort:
    """The same agent as an explicit node-and-edge graph."""

    name = "graph"
    #: The policy check is a node, so it is inserted by declaring an edge
    #: rather than by wrapping a tool.
    policy_hook = True

    def __init__(
        self,
        policy: PolicyEngine | None = None,
        telemetry: object | None = None,
    ) -> None:
        self.policy = policy
        self.telemetry = telemetry
        self.resumed_from_step: int | None = None
        self.vendor_bytes = 0
        self.principal = Principal.of(
            "CUST-8841", "orders:read", "policy:read", "refunds:write"
        )

    # ------------------------------------------------------------- assembly

    def build(
        self,
        model: ModelProvider,
        tools: ToolRegistry,
        specs: list[ToolSpec],
    ) -> None:
        """Declare the state schema's inhabitants, the nodes, and the edges."""
        self.model = model
        self.tools = tools
        self.specs = specs
        # The same file the raw port uses. Rows are keyed by run id, so the
        # two runtimes coexist and neither can read the other's runs.
        self.checkpointer = SqliteCheckpointer(shared.triage.DB_PATH)
        self.max_turns = 12
        self.budget_cents: Money = 200
        self.nodes: dict[str, Node] = {
            "agent": self._agent_node,
            "authorize": self._authorize_node,
            "tools": self._tools_node,
        }
        self.edges: dict[str, Callable[[GraphState], str]] = {
            "agent": lambda s: "authorize" if s.pending else END,
            "authorize": lambda s: "tools" if s.pending else END,
            "tools": lambda s: "agent",
        }
        self.entry = "agent"

    # -------------------------------------------------------------- driving

    def run(self, goal: str, run_id: str) -> RunState:
        """Start at the entry node and follow edges until one returns END."""
        state = GraphState(
            run_id=run_id,
            messages=[
                Message(role="system", content=DEFAULT_SYSTEM_PROMPT),
                Message(role="user", content=goal),
            ],
        )
        self._save(state)
        return self._drive(state, self.entry)

    def resume(self, run_id: str) -> RunState:
        """Resume at the node the runtime was inside when it died.

        A checkpoint written before the tool node means the pending calls
        survive the crash, so the runtime knows *which* call it could not
        confirm rather than having to re-derive it from the transcript.
        """
        saved = self.checkpointer.load(run_id)
        if saved is None:
            raise LookupError(f"no checkpoint for {run_id!r}")
        state = GraphState.of(saved)
        self.resumed_from_step = state.step
        node = "authorize" if state.pending else self.entry
        return self._drive(state, node)

    def _drive(self, state: GraphState, node: str) -> RunState:
        """The runtime: run a node, checkpoint, take the edge, repeat."""
        while node != END:
            state = self.nodes[node](state)
            self._save(state)
            self._emit(state, "node.finished", {"node": node})
            node = self.edges[node](state)
            if state.step >= self.max_turns:
                raise RuntimeError(
                    f"{state.run_id} hit the {self.max_turns}-turn ceiling"
                )
        if state.status == "running":
            state = replace(state, status="succeeded")
            self._save(state)
        return state.as_run_state()

    # ---------------------------------------------------------------- nodes

    def _agent_node(self, state: GraphState) -> GraphState:
        """Ask the model for the next turn and record what it chose."""
        response = self.model.complete(list(state.messages), self.specs)
        cents = int(default_cost_cents(response))
        self._emit(
            state,
            "model.called",
            {"model": response.model, "cost_cents": cents},
        )
        return replace(
            state,
            messages=[*state.messages, response.as_message()],
            step=state.step + 1,
            budget_spent_cents=state.budget_spent_cents + cents,
            pending=list(response.tool_calls),
        )

    def _authorize_node(self, state: GraphState) -> GraphState:
        """A synchronous decision point, expressed as its own node."""
        if self.policy is None:
            return state
        for call in state.pending:
            spec = self.tools.spec_for(call.name)
            ctx: dict[str, Any] = {
                "run_id": state.run_id,
                "step": state.step,
                "budget_spent_cents": state.budget_spent_cents,
                "writes": bool(spec and spec.writes),
            }
            if self.policy.evaluate(self.principal, call, ctx) is Decision.DENY:
                return replace(state, status="failed", pending=[])
        return state

    def _tools_node(self, state: GraphState) -> GraphState:
        """Dispatch every pending call and append the observations."""
        observations: list[Message] = []
        for call in state.pending:
            result = self.tools.dispatch(
                call, run_id=state.run_id, step=state.step - 1
            )
            observations.append(
                Message(
                    role="tool",
                    content={
                        "call_id": result.call_id,
                        "tool": call.name,
                        "ok": result.ok,
                        "truncated": result.truncated,
                        "content": result.content,
                    },
                )
            )
        return replace(
            state,
            messages=[*state.messages, *observations],
            pending=[],
        )

    # ------------------------------------------------------------- plumbing

    def _save(self, state: GraphState) -> None:
        self.checkpointer.save(state.as_run_state())

    def _emit(self, state: GraphState, kind: str, payload: Any) -> None:
        if self.telemetry is None:
            return
        self.telemetry.emit(
            {
                "run_id": state.run_id,
                "step": state.step,
                "type": kind,
                "payload": payload,
            }
        )

    @property
    def checkpoint_writes(self) -> int:
        """Checkpoints this runtime wrote. One per *node*, not per turn."""
        return int(self.checkpointer.writes)

    def close(self) -> None:
        """Release the checkpoint file handle."""
        self.checkpointer.close()
