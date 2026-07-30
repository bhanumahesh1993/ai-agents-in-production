"""Assembling the three stores and the workflow, in exactly one place.

The demo runs in three separate processes and the tests run in one. If each
assembled the stores its own way, the thing the tests prove and the thing the
demo prints would be two different systems, and the interesting differences
would be the ones nobody meant.

Two files, not one, and the split is the design rather than tidiness.
``run.db`` holds everything that is yours: the checkpoint, the envelope, the
transition log, the approval inbox, and the intent journal. ``refunds.db``
stands where a payments provider stands -- a system you do not own, with its
own transaction boundary, which dedupes on the key you present and has no
opinion about whether your total makes sense.

That separation is what makes the broken-key demonstration honest. On one
shared connection the provider's commit would flush your journal too, and the
failure would become impossible to reproduce for a reason that has nothing to
do with the design.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path

from approvals import DurableApprovals
from envelope import EnvelopeStore
from northstar_contracts import World
from northstar_policy import Principal, default_northstar_policy
from pause import ORDER, Workflow, WorkflowConfig
from side_effects import RefundService, SideEffectLedger

__all__ = ["PRINCIPAL", "RUN_DB", "SERVICE_DB", "Wiring", "build", "reset"]

#: Yours: checkpoint, envelope, transitions, approvals, intent journal.
RUN_DB = "run.db"
#: Not yours: the payments provider's own store.
SERVICE_DB = "refunds.db"

#: The identity the run acts under. Three ids, not one, and the scope is
#: the narrowest that lets the work finish.
PRINCIPAL = Principal.of(
    "CUST-9032",
    "orders:read",
    "refunds:write",
    agent_id="northstar-support-agent",
    operator_id="northstar-platform",
)


@dataclass
class Wiring:
    """The stores and the workflow, with one place to close them all."""

    workflow: Workflow
    store: EnvelopeStore
    approvals: DurableApprovals
    ledger: SideEffectLedger
    service: RefundService

    def close(self) -> None:
        """Release every handle on both files."""
        for handle in (self.ledger, self.service, self.approvals, self.store):
            try:
                handle.close()
            except Exception:  # noqa: BLE001 - closing must not mask a result
                pass

    def __enter__(self) -> Wiring:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def reset(state_dir: Path) -> Path:
    """Delete and recreate the state directory.

    Called once by the orchestrator, never by a phase. A phase that wiped
    the state it was supposed to resume from would pass every time.
    """
    if state_dir.exists():
        shutil.rmtree(state_dir)
    state_dir.mkdir(parents=True, exist_ok=True)
    return state_dir


def build(
    state_dir: Path,
    *,
    agent_version: str,
    key_strategy: str = "derived",
    kill_after_settle: bool = False,
) -> Wiring:
    """Open both files and assemble the workflow over them.

    Args:
        state_dir: Directory holding ``run.db`` and ``refunds.db``. It must
            already exist; see :func:`reset`.
        agent_version: What this worker *is*. The resume path compares it
            against what the checkpoint says produced the state.
        key_strategy: ``"derived"`` or ``"generated"``. The whole chapter is
            a few characters of difference here.
        kill_after_settle: Raise ``SimulatedCrash`` once a refund has
            settled and before its outcome is recorded. Test affordance.
    """
    state_dir.mkdir(parents=True, exist_ok=True)
    run_db = state_dir / RUN_DB
    store = EnvelopeStore(run_db)
    approvals = DurableApprovals(run_db)
    ledger = SideEffectLedger(run_db)
    service = RefundService(state_dir / SERVICE_DB)
    config = WorkflowConfig(
        agent_version=agent_version,
        key_strategy=key_strategy,
        kill_after_settle=kill_after_settle,
    )
    workflow = Workflow(
        store=store,
        approvals=approvals,
        ledger=ledger,
        service=service,
        policy=default_northstar_policy(
            threshold_cents=5000, flagged_order_ids=(ORDER,)
        ),
        principal=PRINCIPAL,
        config=config,
        world=World(),
    )
    return Wiring(workflow, store, approvals, ledger, service)
