"""The containment ladder: five rungs, each with a role and a latency.

Containment is not one switch. Rungs one and two are automatic and require
no human at all, which is the point: the great majority of containment
should happen without anyone being paged.

Four rules govern the whole ladder, and each one is checkable rather than
aspirational, which is why they are properties on :class:`Rung` and
assertions in the test suite.

* Every rung is pullable without a code deploy. A containment action that
  requires a build is not available in the ten minutes you need it.
* Authorization friction *decreases* as you climb. A kill switch that
  requires a change-approval board is not a kill switch.
* Every rung's latency is measured, not assumed. "We can disable writes"
  means something different at 30 seconds and at 30 minutes.
* Every rung is tested on a schedule, in production. The rung nobody tests
  is invariably the one that quietly depended on a decommissioned service.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

__all__ = [
    "LADDER",
    "ContainmentLog",
    "PauseMode",
    "Rung",
    "Tripwire",
    "friction_decreases",
    "untested",
]

PauseMode = Literal["drain", "read_only", "stop"]


@dataclass(frozen=True)
class Rung:
    """One rung of the ladder.

    Args:
        name: What you say on the call.
        stops: The blast radius this rung ends.
        role: Who may pull it. A role, never a person.
        friction: Authorization steps needed, 0 for automatic. This must
            not increase as you climb.
        latency_seconds: Measured, not assumed. ``None`` means nobody has
            measured it, which is itself a finding.
        needs_deploy: Whether pulling it requires a build. Must be
            ``False`` on every rung.
        tested_every_days: Test cadence. ``None`` means untested.
    """

    name: str
    stops: str
    role: str
    friction: int
    latency_seconds: float | None
    needs_deploy: bool = False
    tested_every_days: int | None = 90


#: Northstar's ladder. Below rung five sit the credential-level actions —
#: revoking the agent's identity and its delegations — which are the only
#: ones that also stop anything already holding a valid token.
LADDER: tuple[Rung, ...] = (
    Rung("deny_call", "one action", "policy engine, automatic", 0, 0.001),
    Rung("per_run_budget", "one run", "runtime, at admission", 0, 0.001),
    Rung("pause_agent", "all runs of one agent", "service on-call", 2, 30.0),
    Rung("roll_back_version", "one bad release", "release owner", 1, 240.0),
    Rung("fleet_kill_switch", "every agent, all writes", "security on-call",
         0, 15.0),
)


def friction_decreases(ladder: tuple[Rung, ...] = LADDER) -> bool:
    """Whether friction never rises between rung three and the top.

    Rungs one and two are automatic and sit outside the comparison: they
    have no authorization step because no human is involved. From rung
    three upward, each rung must be at most as hard to pull as the one
    below it.
    """
    human = [r for r in ladder if r.friction > 0 or r.role.endswith("call")]
    return all(
        a.friction >= b.friction for a, b in zip(human, human[1:], strict=False)
    )


def untested(ladder: tuple[Rung, ...] = LADDER) -> list[str]:
    """Rungs with no test cadence, and rungs with no measured latency."""
    return [
        r.name
        for r in ladder
        if r.tested_every_days is None or r.latency_seconds is None
    ]


class ContainmentLog:
    """The five rungs as callable operations, with an audit event on each.

    Every method returns the rung it pulled and appends a record. A
    containment action nobody recorded is a containment action nobody can
    review afterwards, and the review is where you find out that rung three
    took eleven minutes.

    Example:
        >>> log = ContainmentLog()
        >>> log.pause_agent("northstar-support", "read_only", by="sre:oncall")
        'pause_agent'
        >>> log.records[0]["mode"]
        'read_only'
    """

    def __init__(self) -> None:
        self.records: list[dict[str, Any]] = []
        #: Agents currently paused, and the degree.
        self.paused: dict[str, PauseMode] = {}
        #: Whether the fleet-wide switch is pulled.
        self.fleet_stopped = False

    def _pull(self, rung: str, by: str, **detail: Any) -> str:
        """Record one rung being pulled and return its name."""
        entry = next(r for r in LADDER if r.name == rung)
        self.records.append(
            {
                "rung": rung,
                "stops": entry.stops,
                "by": by,
                "friction": entry.friction,
                "latency_seconds": entry.latency_seconds,
                **detail,
            }
        )
        return rung

    def deny_call(self, tool: str, reason: str) -> str:
        """Rung one: the policy engine refused one action. Automatic."""
        return self._pull(
            "deny_call", "policy-engine", tool=tool, reason=reason
        )

    def per_run_budget(self, run_id: str, kind: str) -> str:
        """Rung two: a hard cap ended one run. Automatic, at admission."""
        return self._pull("per_run_budget", "runtime", run_id=run_id,
                          kind=kind)

    def pause_agent(self, agent_id: str, mode: PauseMode, by: str) -> str:
        """Rung three: stop one agent, in one of three useful degrees.

        ``drain`` stops admitting new runs and lets current ones finish.
        ``read_only`` disables writes and keeps reads, so in-flight work can
        complete without moving money. ``stop`` ends everything. Knowing
        which you need is worth deciding before the incident.
        """
        self.paused[agent_id] = mode
        return self._pull("pause_agent", by, agent_id=agent_id, mode=mode)

    def roll_back_version(self, agent_id: str, to_version: str,
                          by: str) -> str:
        """Rung four: a normal release operation, and usually the right one.

        It belongs on this ladder rather than above it: rolling back a bad
        agent version is a containment action, not an escalation beyond one.
        """
        return self._pull(
            "roll_back_version", by, agent_id=agent_id, to_version=to_version
        )

    def fleet_kill_switch(self, by: str, reason: str) -> str:
        """Rung five: every agent, all writes. Named rota, no approval."""
        self.fleet_stopped = True
        return self._pull("fleet_kill_switch", by, reason=reason)

    def writes_allowed(self, agent_id: str) -> bool:
        """Whether this agent may still write, given what has been pulled."""
        if self.fleet_stopped:
            return False
        return self.paused.get(agent_id) not in ("read_only", "stop")

    def rungs_pulled(self) -> list[str]:
        """Every rung pulled, in order."""
        return [r["rung"] for r in self.records]


@dataclass
class Tripwire:
    """A detector that raises the containment level. Never a gate.

    A gate is a deterministic check with a defined answer: this principal,
    this scope, this amount, allow or deny. A tripwire fires on a signal it
    cannot fully characterise. The correct response to one is to raise the
    containment level, not to be the last thing between the agent and the
    world, and it should never be the reason a call was *allowed*.
    """

    name: str
    #: What firing does. Notably absent: "allow".
    raises_to: Literal["require_approval", "read_only", "pause"] = (
        "require_approval"
    )
    fired: list[str] = field(default_factory=list)

    def fire(self, log: ContainmentLog, agent_id: str, signal: str) -> str:
        """Record the signal and raise the containment level."""
        self.fired.append(signal)
        if self.raises_to == "read_only":
            return log.pause_agent(agent_id, "read_only", by=self.name)
        if self.raises_to == "pause":
            return log.pause_agent(agent_id, "drain", by=self.name)
        return "require_approval"
