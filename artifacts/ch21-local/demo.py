"""The local stack, checked and then exercised.

    python artifacts/ch21-local/demo.py

Validates the Compose file by parsing it, runs the Northstar damaged-item
task end to end in mock mode with the timeout fault injected, prints the
event log and the resulting ledger, and exits non-zero if the ledger holds
more than one refund for the order.

**One honest deviation.** The chapter says `make demo-ch21` brings the
Compose stack up. This demo does not start Docker: no chapter demo in this
repository may require a daemon, a network, or a credential, and CI runs
with none of the three. So the nine services are validated by parsing the
file rather than by applying it, and the task runs in process against the
same MCP gateway, the same policy bundle, and the same world the composed
stack would use. `make local-up` is the command that starts containers, and
it is the one thing here you cannot verify offline.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from datetime import date

import cassettes
import faults
import stack
from local_model import PROMOTION_CHECKS, describe, unmet
from mcp_server import PROTOCOL_REVISION, SUPPORT_PRINCIPAL
from model_mode import MODES, mode_from_env
from run_local import AMOUNT, ORDER, run_task

SCRIPT_DIR = Path(__file__).resolve().parent / "scripts"


def check_stack(failures: list[str]) -> None:
    """Nine services, every image pinned, no dangling dependency."""
    print("\n=== the local reference stack ===")
    document = stack.load_compose()
    services = document["services"]
    for name, role in stack.REQUIRED_SERVICES.items():
        mark = "ok " if name in services else "NO "
        print(f"  {mark} {name:<11} {role}")
    found = stack.problems()
    print(f"  images pinned by digest: "
          f"{not stack.unpinned_images(stack.load_env())}")
    print(f"  problems: {found or 'none'}")
    failures.extend(found)


def check_modes(failures: list[str]) -> None:
    """Four modes, and the default that matters most."""
    print("\n=== model modes ===")
    default = mode_from_env({})
    print(f"  MODEL_MODE unset -> {default}")
    print(f"  known modes      : {', '.join(MODES)}")
    unknown = ""
    try:
        mode_from_env({"MODEL_MODE": "liv"})
    except ValueError as exc:
        unknown = str(exc)
    print(f"  a typo           : {unknown or 'accepted, which is the bug'}")

    if default != "mock":
        failures.append(f"MODEL_MODE defaults to {default!r}, not 'mock'")
    if not unknown:
        failures.append("an unknown MODEL_MODE was accepted")


def check_cassettes(failures: list[str]) -> None:
    """Cassettes are data exports with a shelf life."""
    print("\n=== cassettes ===")
    cassette = cassettes.load(SCRIPT_DIR / "refund.jsonl")
    leaked = cassettes.unredacted(SCRIPT_DIR / "refund.jsonl")
    print(f"  file           : {cassette.path.name}")
    print(f"  model          : {cassette.model} via {cassette.provider}")
    print(f"  recorded_at    : {cassette.recorded_at}")
    print(f"  unredacted keys: {leaked or 'none'}")

    stale = cassette.is_expired(date(2027, 1, 1))
    print(f"  expired on 2027-01-01: {stale} "
          f"(the shelf life is {cassettes.MAX_AGE_DAYS} days)")

    if leaked:
        failures.append(f"the cassette carries unredacted keys: {leaked}")
    if not stale:
        failures.append("an eight-month-old cassette did not age out")


def check_faults(failures: list[str]) -> None:
    """Six catalogued failures, each with a distinct correct response."""
    print("\n=== the fault catalogue ===")
    for fault in faults.catalogue():
        producible = "world" if fault.world_kind else "not producible here"
        print(f"  {fault.name:<14} {producible:<20} {fault.response[:44]}")
    missing = faults.unsupported()
    print(f"  named but not producible by the in-memory world: {missing}")

    if set(missing) != {"expired_token", "partial"}:
        failures.append(
            f"the unsupported-fault list moved: {missing}"
        )


def smoke_run(failures: list[str]) -> None:
    """The damaged-item task, with the timeout injected on purpose."""
    print("\n=== make demo-ch21: the damaged-item task ===")
    run = run_task(mode="mock", inject="timeout")
    print(f"gateway    listening: in-process MCP {PROTOCOL_REVISION}  "
          f"({len(run.server.registry.specs())} tools)")
    print(f"agent      admission ok  {run.state.run_id}  tenant=northstar")
    for line in run.trace():
        print(line)
    print(f"ledger     {ORDER}  refunds={run.refund_rows}  "
          f"total_cents={run.refunded_cents}")
    print(f"run        {run.state.run_id}  {run.state.status}  "
          f"{run.state.step} steps  {run.model_cents} estimated model cents")
    print(f"principal  {SUPPORT_PRINCIPAL.agent_id} "
          f"scopes={sorted(SUPPORT_PRINCIPAL.scopes)}")
    print(f"attempts   issue_refund x{run.tool_attempts('issue_refund')}, "
          f"and the ledger still holds {run.refund_rows}")

    if run.refund_rows != 1:
        failures.append(
            f"the ledger holds {run.refund_rows} refunds for {ORDER}"
        )
    if run.refunded_cents != AMOUNT:
        failures.append(
            f"the ledger holds {run.refunded_cents}c, want {AMOUNT}c"
        )
    if run.state.status != "succeeded":
        failures.append(f"the run ended {run.state.status!r}")
    if run.tool_attempts("issue_refund") < 2:
        failures.append("the injected timeout produced no retry to survive")


def replay_run(failures: list[str]) -> None:
    """Same scenario, recorded script, byte-identical ledger."""
    print("\n=== MODEL_MODE=replay ===")
    mock = run_task(mode="mock", inject="timeout")
    replay = run_task(mode="replay", inject="timeout")
    print(f"  mock   : {mock.refund_rows} refund(s), "
          f"{mock.refunded_cents} cents")
    print(f"  replay : {replay.refund_rows} refund(s), "
          f"{replay.refunded_cents} cents")

    if (mock.refund_rows, mock.refunded_cents) != (
        replay.refund_rows,
        replay.refunded_cents,
    ):
        failures.append("mock and replay produced different ledgers")


def local_models(failures: list[str]) -> None:
    """Never promote a local model on text quality."""
    print("\n=== local inference: what to test before you promote ===")
    for row in describe():
        print(f"  {row['server']:<11} {row['role']:<32} watch: "
              f"{row['watch']}")
    outstanding = unmet({"json_and_schema_validity_under_real_schemas": True})
    print(f"  checks run: 1 of {len(PROMOTION_CHECKS)}; "
          f"{len(outstanding)} outstanding")

    if len(outstanding) != len(PROMOTION_CHECKS) - 1:
        failures.append("a promotion check nobody ran counted as passing")


def main() -> int:
    print("Chapter 21 — the local stack, offline, with no credentials")

    failures: list[str] = []
    check_stack(failures)
    check_modes(failures)
    check_cassettes(failures)
    check_faults(failures)
    smoke_run(failures)
    replay_run(failures)
    local_models(failures)

    print("\n--- what this proves ---")
    print("The complete agent system — tool boundary, policy enforcement,")
    print("event log, and world — runs on one machine with no credentials")
    print("and no network, and a specific partial failure is reproducible")
    print("on demand rather than waited for.")
    print("\nThe Compose file is validated by parsing, not by applying:")
    print("no demo in this repository may require a daemon. `make local-up`")
    print("is the command that starts the nine containers.")

    if failures:
        print("\nFAILED:")
        for line in failures:
            print(f"  - {line}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
