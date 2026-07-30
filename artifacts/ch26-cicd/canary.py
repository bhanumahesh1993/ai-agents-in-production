"""Independent containment flags, and a canary that widens by cohort.

Two ideas, and the first one is the load-bearing one.

**Flags are independent, so containment needs no deploy.** "Roll back" is
too coarse an instrument at 03:12. Turning off new admissions, one tool,
every mutation, external egress, memory writes, or one agent version are
six different actions with six different blast radii, and each is smaller,
faster, and more reversible than a deploy.

**Canary along the axis that carries the risk.** A 5% random canary on a
system where 0.5% of runs touch money tells you almost nothing about the
money. The stages below widen by cohort *and* by what the agent is allowed
to do: reads first, then bounded writes with an amount ceiling, then
general writes.

Nothing in this module knows about the agent. It takes SLO readings as
data and returns a decision, which is what makes it testable without a
deployment and swappable for whatever your platform actually runs.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from northstar_contracts import Money, ToolSpec

__all__ = [
    "COHORTS",
    "FLAGS",
    "CanaryController",
    "CanaryStage",
    "FlagSet",
    "SloReading",
    "SloTargets",
]

#: The containment ladder, as independent flags. Every one of these is a
#: separate switch on purpose.
FLAGS = {
    "admit_new_runs": True,
    "tool:issue_refund": True,
    "all_mutations": True,
    "external_egress": True,
    "memory_writes": True,
    "agent_version:v9": True,
}


@dataclass
class FlagSet:
    """Independent kill switches, consulted at the action boundary.

    A flag consulted only at admission stops new work and leaves in-flight
    runs mutating, which is the failure a real drill finds. ``allows_call``
    is therefore what the tool gate calls on *every* dispatch.

    Attributes:
        values: Flag name to enabled. Names not present default to
            enabled, so a new tool is not silently unreachable.
        actions: Append-only record of every flip, with its reason. A
            containment action nobody wrote down is one the postmortem
            cannot reconstruct.
    """

    values: dict[str, bool] = field(default_factory=lambda: dict(FLAGS))
    actions: list[dict[str, object]] = field(default_factory=list)

    def enabled(self, name: str) -> bool:
        """Whether ``name`` is on. Unknown flags are on."""
        return self.values.get(name, True)

    def disable(self, name: str, reason: str = "") -> None:
        """Pull one switch. Takes effect on the next call, not the next
        deploy."""
        self.values[name] = False
        self.actions.append(
            {"flag": name, "enabled": False, "reason": reason}
        )

    def enable(self, name: str, reason: str = "") -> None:
        """Put one switch back."""
        self.values[name] = True
        self.actions.append({"flag": name, "enabled": True, "reason": reason})

    def allows_run(self, version: str) -> tuple[bool, str]:
        """Whether a new run of ``version`` may be admitted."""
        if not self.enabled("admit_new_runs"):
            return False, "admission is closed"
        if not self.enabled(f"agent_version:{version}"):
            return False, f"agent version {version} is disabled"
        return True, ""

    def allows_call(
        self,
        spec: ToolSpec,
        *,
        amount_cents: Money | None = None,
        ceiling_cents: Money | None = None,
    ) -> tuple[bool, str]:
        """Whether one tool call may be dispatched, and why not.

        Checked per call, at the action boundary, so a flag flipped while
        a run is halfway through its trajectory stops the *next* thing that
        run tries to do to the world.
        """
        if not self.enabled(f"tool:{spec.name}"):
            return False, f"tool {spec.name} is disabled"
        if spec.writes and not self.enabled("all_mutations"):
            return False, "all mutations are disabled"
        if spec.name == "send_message" and not self.enabled("external_egress"):
            return False, "external egress is disabled"
        if (
            spec.writes
            and ceiling_cents is not None
            and amount_cents is not None
            and amount_cents > ceiling_cents
        ):
            return False, (
                f"{amount_cents}c is over this stage's {ceiling_cents}c "
                f"write ceiling"
            )
        return True, ""


@dataclass(frozen=True)
class CanaryStage:
    """One rung: who sees the candidate, and what it may do to the world."""

    cohort: str
    writes_enabled: bool
    ceiling_cents: Money | None = None

    def describe(self) -> str:
        """One line for the release log."""
        if not self.writes_enabled:
            return f"{self.cohort}: reads only"
        if self.ceiling_cents is None:
            return f"{self.cohort}: writes, no ceiling"
        return f"{self.cohort}: writes up to {self.ceiling_cents}c"


#: Reads before writes, bounded writes before general ones.
COHORTS: tuple[CanaryStage, ...] = (
    CanaryStage("internal", writes_enabled=False),
    CanaryStage("beta", writes_enabled=True, ceiling_cents=2000),
    CanaryStage("ten_percent", writes_enabled=True, ceiling_cents=5000),
    CanaryStage("all", writes_enabled=True, ceiling_cents=None),
)


@dataclass(frozen=True)
class SloTargets:
    """What the canary has to hold to widen.

    Both of these are agent SLIs, not service SLIs. Availability and error
    rate can be perfect while the agent is confidently wrong.
    """

    verified_success: float = 0.90
    action_integrity: float = 0.99


@dataclass(frozen=True)
class SloReading:
    """What one cohort actually did. Every field is a count."""

    cohort: str
    runs: int
    verified_successes: int
    mutations_attempted: int
    mutations_correct: int

    @property
    def verified_success(self) -> float:
        """Successful authoritative outcomes over completed runs."""
        return self.verified_successes / self.runs if self.runs else 0.0

    @property
    def action_integrity(self) -> float:
        """Correct mutations over all attempted mutations.

        The denominator counts mutations rather than requests, which is
        what makes this the SLI that catches a duplicate-refund incident.
        """
        if not self.mutations_attempted:
            return 1.0
        return self.mutations_correct / self.mutations_attempted

    def breaches(self, targets: SloTargets) -> list[str]:
        """Every target this reading missed, named."""
        out: list[str] = []
        if self.verified_success < targets.verified_success:
            out.append(
                f"verified success {self.verified_success:.2f} < "
                f"{targets.verified_success:.2f}"
            )
        if self.action_integrity < targets.action_integrity:
            out.append(
                f"action integrity {self.action_integrity:.2f} < "
                f"{targets.action_integrity:.2f}"
            )
        return out


class CanaryController:
    """Widens exposure while the SLOs hold, and contains when they do not.

    Args:
        flags: The switch set to pull when a reading breaches.
        targets: What the candidate has to hold.
    """

    def __init__(
        self,
        flags: FlagSet,
        targets: SloTargets = SloTargets(),
    ) -> None:
        self.flags = flags
        self.targets = targets
        self.index = 0
        self.contained = False
        self.history: list[dict[str, object]] = []

    @property
    def stage(self) -> CanaryStage:
        """The rung the canary is on."""
        return COHORTS[min(self.index, len(COHORTS) - 1)]

    @property
    def complete(self) -> bool:
        """Whether the candidate has reached full exposure."""
        return self.index >= len(COHORTS) - 1 and not self.contained

    def observe(self, reading: SloReading) -> str:
        """Judge one cohort's readings and act.

        Returns:
            ``"widened"``, ``"held"`` when there is nowhere further to go,
            or ``"contained"``.
        """
        breaches = reading.breaches(self.targets)
        if breaches:
            self.contained = True
            self.flags.disable(
                "all_mutations", reason="; ".join(breaches)
            )
            outcome = "contained"
        elif self.index < len(COHORTS) - 1:
            self.index += 1
            outcome = "widened"
        else:
            outcome = "held"
        self.history.append(
            {
                "cohort": reading.cohort,
                "verified_success": round(reading.verified_success, 4),
                "action_integrity": round(reading.action_integrity, 4),
                "outcome": outcome,
                "breaches": breaches,
            }
        )
        return outcome
