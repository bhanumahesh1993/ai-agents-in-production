"""Port three: a hosted harness, and the loop you no longer own.

``HostedAgent`` stands in for the managed runtimes in the chapter's survey.
It is not a mock of any particular product; it is the set of decisions those
products have in common, made explicit so they can be measured:

* the loop's internals are not yours — you pass a model, tools, and a
  permission list, and you get a run back;
* permissions are the extension point, so a synchronous check of your own
  between selection and execution is not available;
* the session is persisted, and the session is the conversation, not the
  execution state, so a restart replays rather than resumes;
* payload capture is on by default, and the payload includes tool
  arguments.

None of those is a scandal. Every one of them is a decision somebody made
for you, and criterion seven says you should be able to name them.
"""

from __future__ import annotations

from typing import Any

from northstar_contracts import (
    RunState,
    ToolSpec,
    canonical_json,
)
from northstar_policy import (
    Decision,
    PolicyEngine,
    Principal,
    Rule,
    RulesPolicyEngine,
)
from northstar_runtime import AgentLoop, ModelProvider, ToolRegistry

__all__ = [
    "SESSIONS",
    "HarnessPort",
    "HostedAgent",
    "VendorSink",
    "reset_sessions",
]

#: The vendor's session store. It is hosted, so it outlives your process
#: and a new client object finds the session again — which is exactly why
#: teams read it as durable execution. Read what it holds: a goal and a
#: finished result, and nothing about a run that was interrupted.
SESSIONS: dict[str, dict[str, Any]] = {}


def reset_sessions() -> None:
    """Clear the hosted store, so one measurement cannot see another's."""
    SESSIONS.clear()

#: What each permission string grants. The harness's whole authorisation
#: model: a fixed vocabulary, and no hook for a rule of your own.
PERMISSION_TOOLS: dict[str, tuple[str, ...]] = {
    "orders:read": ("get_order", "search_orders"),
    "policy:read": ("get_policy",),
    "refunds:write": ("issue_refund",),
    "messages:write": ("send_message",),
}


class VendorSink:
    """The harness vendor's trace collector, which is off your process.

    Bytes counted here are bytes that left. That is the measurement
    criterion seven asks for, and it is why this class exists rather than a
    boolean called ``telemetry_enabled``.
    """

    def __init__(self, capture_payloads: bool = True) -> None:
        self.capture_payloads = capture_payloads
        self.records: list[dict[str, Any]] = []

    def emit(self, record: dict[str, Any]) -> None:
        """Accept one event, redacting arguments only if asked to."""
        payload = dict(record.get("payload") or {})
        if not self.capture_payloads:
            payload.pop("arguments", None)
        self.records.append({**record, "payload": payload})

    @property
    def argument_bytes(self) -> int:
        """Bytes of tool arguments this sink received."""
        total = 0
        for record in self.records:
            arguments = (record.get("payload") or {}).get("arguments")
            if arguments is not None:
                total += len(canonical_json(arguments))
        return total


def _permission_policy(permissions: tuple[str, ...]) -> PolicyEngine:
    """Turn a permission list into the only decision point on offer."""
    granted = {
        tool for p in permissions for tool in PERMISSION_TOOLS.get(p, ())
    }
    return RulesPolicyEngine(
        rules=[
            Rule(
                name="harness.permission",
                when=lambda p, c, ctx: c.name not in granted,
                decision=Decision.DENY,
                reason="tool is outside the agent's permission list",
            )
        ],
        default=Decision.ALLOW,
        default_reason="tool is in the agent's permission list",
    )


class HostedAgent:
    """A managed agent runtime. You do not see the loop."""

    def __init__(
        self,
        model: ModelProvider,
        tools: ToolRegistry,
        permissions: tuple[str, ...] = (),
        *,
        capture_payloads: bool = True,
    ) -> None:
        self.vendor = VendorSink(capture_payloads=capture_payloads)
        self._loop = AgentLoop(
            model,
            tools,
            policy=_permission_policy(permissions),
            telemetry=self.vendor,
            max_turns=12,
            budget_cents=200,
            principal=Principal.of("CUST-8841", *permissions),
        )
        #: Session storage. The *conversation*, keyed by session id — not
        #: the execution state, and not written until the run finishes.
        self.sessions = SESSIONS
        self.session_writes = 0

    def invoke(self, goal: str, session: str) -> RunState:
        """Run to completion and persist the session afterwards."""
        self.sessions.setdefault(session, {"goal": goal, "state": None})
        state = self._loop.run(goal, run_id=session)
        self.sessions[session]["state"] = state
        self.session_writes += 1
        return state

    def invoke_from_session(self, session: str) -> RunState:
        """Continue a session.

        There is no mid-run execution state to continue from, because none
        was written, so this replays the goal. The harness calls that
        resumption. It is resumption of the conversation.
        """
        record = self.sessions.get(session)
        if record is None:
            raise LookupError(f"no session {session!r}")
        finished = record.get("state")
        if finished is not None:
            return finished
        return self.invoke(str(record["goal"]), session)


class HarnessPort:
    """A hosted harness, behind the shared port."""

    name = "harness"
    #: Permissions are the extension point. A synchronous check of your own
    #: between tool selection and tool execution is not on offer, so the
    #: honest score on criterion one is a no.
    policy_hook = False

    def __init__(
        self,
        policy: PolicyEngine | None = None,
        telemetry: object | None = None,
    ) -> None:
        # ``policy`` is accepted and dropped on purpose: this is what
        # "you can only wrap the tool" looks like from the caller's side.
        self.rejected_policy = policy
        self.telemetry = telemetry
        self.resumed_from_step: int | None = None

    def build(
        self,
        model: ModelProvider,
        tools: ToolRegistry,
        specs: list[ToolSpec],
    ) -> None:
        self.agent = HostedAgent(
            model, tools,
            permissions=("orders:read", "policy:read", "refunds:write"),
        )

    def run(self, goal: str, run_id: str) -> RunState:
        return self.agent.invoke(goal, session=run_id)

    def resume(self, run_id: str) -> RunState:
        self.resumed_from_step = 0
        return self.agent.invoke_from_session(run_id)

    @property
    def checkpoint_writes(self) -> int:
        """Sessions written. One per finished run, not one per step."""
        return self.agent.session_writes

    @property
    def vendor_bytes(self) -> int:
        """Bytes of tool arguments that left the process by default."""
        return self.agent.vendor.argument_bytes

    def close(self) -> None:
        """Nothing local to release: the state is not on your machine."""
        return None
