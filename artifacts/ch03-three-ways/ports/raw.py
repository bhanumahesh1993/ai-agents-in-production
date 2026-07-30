"""Port one: the Chapter 2 loop, wearing the port's signature.

The control group. Everything the loop does is in this repository and every
seam between the model's decision and the tool's execution is a line you
can put a breakpoint on. That is the whole of what it buys, and the
scorecard's ``glue`` column is what it costs.
"""

from __future__ import annotations

from northstar_contracts import RunState, ToolSpec
from northstar_policy import PolicyEngine, Principal
from northstar_runtime import AgentLoop, ModelProvider, SqliteCheckpointer, ToolRegistry

__all__ = ["RawLoopPort"]


class RawLoopPort:
    """The Chapter 2 loop, behind the shared port."""

    name = "raw"
    #: A synchronous decision point sits between tool selection and tool
    #: execution, and it is a constructor argument rather than a wrapper.
    policy_hook = True

    def __init__(
        self,
        policy: PolicyEngine | None = None,
        telemetry: object | None = None,
    ) -> None:
        self.policy = policy
        self.telemetry = telemetry
        self.resumed_from_step: int | None = None
        #: Bytes of tool arguments this runtime sends off-process by
        #: default. Nothing leaves a loop you wrote.
        self.vendor_bytes = 0

    def build(self, model: ModelProvider,
              tools: ToolRegistry,
              specs: list[ToolSpec]) -> None:
        self.loop = AgentLoop(
            model, tools,
            checkpointer=SqliteCheckpointer("ch03.db"),
            max_turns=12, budget_cents=200,
        )
        self.loop.policy = self.policy
        self.loop.telemetry = self.telemetry
        self.loop.principal = Principal.of(
            "CUST-8841", "orders:read", "policy:read", "refunds:write"
        )

    def run(self, goal: str, run_id: str) -> RunState:
        # The printed excerpt elides ``run_id=``. It is load-bearing: a
        # generated run id makes the derived key a nonce, and the replay in
        # test_equivalence.py would then pay twice.
        return self.loop.run(goal, run_id=run_id)

    def resume(self, run_id: str) -> RunState:
        """Pick the run up from the checkpoint file, on a fresh object.

        The checkpoint boundary is the *step*, so a worker killed inside a
        turn resumes at the start of that turn and re-dispatches the call it
        could not confirm. The derived key is what makes that safe.
        """
        state = self.loop.checkpointer.load(run_id)
        if state is None:
            raise LookupError(f"no checkpoint for {run_id!r}")
        self.resumed_from_step = state.step
        return self.loop.resume(state)

    @property
    def checkpoint_writes(self) -> int:
        """Checkpoints this runtime wrote. One per step."""
        return int(self.loop.checkpointer.writes)

    def close(self) -> None:
        """Release the checkpoint file handle."""
        self.loop.checkpointer.close()
