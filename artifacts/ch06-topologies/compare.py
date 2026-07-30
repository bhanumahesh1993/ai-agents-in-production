"""Run all three configurations and produce the comparison table.

Every number here comes out of the run that just happened: turn counts from
each component's ``model.called`` events, token totals from the shared cost
ledger those events feed, and the owner column from the trace record of the
``issue_refund`` observation that failed.

The third row is the point of the chapter. Same agents, same tools, same
fault, differing only in whether the handoff carried its provenance.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import supervisor
import swarm
import topology
from handoff import Handoff, refund_key
from topology import ORDER_ID, REFUND_CENTS, Trace

__all__ = ["CONFIGURATIONS", "TraceRow", "compare", "print_table"]

CONFIGURATIONS = (
    "supervisor",
    "swarm, contract carried",
    "swarm, contract dropped",
)


@dataclass(frozen=True)
class TraceRow:
    """One configuration, measured."""

    name: str
    turns: int
    tokens: int
    turns_by_component: dict[str, int]
    owner: str
    owner_step: int
    anchored: bool
    refund_rows: int
    refunded_cents: int
    keys_presented: tuple[str, ...]
    approvals: int
    status: str
    handoff: Handoff | None = None

    @property
    def distinct_keys(self) -> int:
        """How many distinct identities the refund service was shown."""
        return len(set(self.keys_presented))


def _owner(trace: Trace) -> tuple[str, int]:
    """Which component was holding the write when the timeout came back."""
    if not trace.timeouts:
        return ("(no timeout observed)", -1)
    first = trace.timeouts[0]
    return (first.component, first.step)


def _row(
    name: str,
    trace: Trace,
    world: object,
    keys: tuple[str, ...],
    status: str,
    anchor: str | None,
    handoff: Handoff | None = None,
) -> TraceRow:
    """Assemble one table row from the trace and the authoritative store."""
    owner, step = _owner(trace)
    rows = [
        e for e in world.ledger  # type: ignore[attr-defined]
        if e["kind"] == "refund_issued" and e["order_id"] == ORDER_ID
    ]
    return TraceRow(
        name=name,
        turns=trace.turns,
        tokens=trace.tokens,
        turns_by_component=dict(trace.turns_by_component),
        owner=owner,
        owner_step=step,
        anchored=bool(anchor and all(k == anchor for k in keys)),
        refund_rows=len(rows),
        refunded_cents=sum(int(e["amount_cents"]) for e in rows),
        keys_presented=keys,
        approvals=trace.approvals,
        status=status,
        handoff=handoff,
    )


def compare() -> list[TraceRow]:
    """Run the three configurations, each against its own faulted world."""
    rows: list[TraceRow] = []

    world = topology.build_world()
    state, trace, registry = supervisor.run_supervisor(world)
    rows.append(
        _row(
            CONFIGURATIONS[0],
            trace,
            world,
            tuple(registry.keys_presented),
            state.status,
            anchor=topology.idempotency_key(
                supervisor.SUPERVISOR_RUN_ID, topology.ORIGIN_STEP_ID
            ),
        )
    )

    for name, carry in (
        (CONFIGURATIONS[1], True),
        (CONFIGURATIONS[2], False),
    ):
        world = topology.build_world()
        run = swarm.run_swarm(world, carry_contract=carry)
        keys = tuple(run.registry.keys_presented) if run.registry else ()
        anchor = refund_key(run.handoff) if (carry and run.handoff) else None
        rows.append(
            _row(
                name,
                run.trace,
                world,
                keys,
                run.fraud.status if run.fraud else "not reached",
                anchor=anchor,
                handoff=run.handoff,
            )
        )
    return rows


def print_table(rows: list[TraceRow]) -> None:
    """The comparison table, with tokens relative to the first row."""
    if not rows:
        return
    base = rows[0].tokens or 1
    width = max(len(r.name) for r in rows)
    header = (
        f"{'Run':<{width}}  {'Turns':>5}  {'Tokens':>7}  {'Rel':>6}  "
        f"{'Tok/turn':>8}  {'Rows':>4}  {'Cents':>6}  Owner at the timeout"
    )
    print(header)
    print("-" * (len(header) + 8))
    for r in rows:
        anchor = "origin-anchored" if r.anchored else "no origin anchor"
        owner = (
            f"{r.owner}, step {r.owner_step} ({anchor})"
            if r.owner_step >= 0
            else r.owner
        )
        print(
            f"{r.name:<{width}}  {r.turns:>5}  {r.tokens:>7}  "
            f"{r.tokens / base:>5.2f}x  {r.tokens // max(1, r.turns):>8}  "
            f"{r.refund_rows:>4}  {r.refunded_cents:>6}  {owner}"
        )
    print()
    print(f"the claim was {REFUND_CENTS} cents, once")
    print(
        "tok/turn is where the transfer shows up: a swarm turn carries the "
        "accumulated\ntranscript, a supervisor's worker turn carries a "
        "scoped brief."
    )


def print_handoff(row: TraceRow) -> None:
    """Print one transfer payload field by field."""
    if row.handoff is None:
        print(f"  {row.name}: no handoff (control never left the loop)")
        return
    print(f"  {row.name}:")
    for field_name, value in row.handoff.to_dict().items():
        print(f"    {field_name:<26} {value}")
