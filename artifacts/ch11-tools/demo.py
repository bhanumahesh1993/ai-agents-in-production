"""The Northstar tool library, its conformance suite, and the lint that fails.

    python artifacts/ch11-tools/demo.py

Eight typed tools registered through a gate that refuses a non-conformant
contract, the token accounting for the shaped and unshaped versions of
``search_orders``, the side-effect ledger from a refund run with a timeout
injected, and the golden-trajectory gate that the opening incident's four-line
description diff has to fail.

Exits non-zero on any conformance failure, on any oversized or unflagged
result from the shaped library, if the lint fails to catch the unshaped
search, if a timeout leaves the ledger without an unresolved intent, if a
dry run moves money, or if the trajectory gate passes on the drifted
description.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from typing import Any

from budget import count_tokens, enforce_budget, shape
from conformance import (
    DESCRIPTION_BUDGET,
    ConformanceError,
    ConformingRegistry,
    check_library,
)
from golden import (
    DAMAGED_TICKET,
    LAMP_SHADE_CENTS,
    ORDER,
    ORDER_TOTAL_CENTS,
    ReadsTheDescription,
    outcome_gate,
    path_of,
    trajectory_gate,
)
from library import build_library, unshaped_search_orders_of
from lint_results import FIXTURES, ResultProbe, bloated_world, lint
from northstar_contracts import (
    ToolCall,
    ToolSpec,
    World,
    idempotency_key,
)
from northstar_runtime import AgentLoop
from refund import SideEffectLedger, cancel_refund, preview_refund
from sandbox import NullSandbox, SandboxContract, SandboxDenied
from specs import (
    APPROVAL_REQUIRED,
    COMPENSATIONS,
    ISSUE_REFUND,
    SEARCH_ORDERS,
    SEARCH_ORDERS_DRIFTED,
    SPECS,
)

APPROVAL_THRESHOLD_CENTS = 5000
RUN_ID = "run-ch11-tools"


def banner(title: str) -> None:
    """Print a section header."""
    print(f"\n=== {title} ===")


# ------------------------------------------------------------ 1. the library


def show_library(failures: list[str]) -> Any:
    """Register the eight tools and print the contract each one declares."""
    banner("the library")
    library = build_library()
    print(f"  {'tool':<24} {'w':>1} {'idem':>4} {'cap':>5}  ver  compensation")
    for spec in library.specs():
        compensation = COMPENSATIONS.get(spec.name)
        inverse = compensation.tool if compensation else "-"
        if spec.name in APPROVAL_REQUIRED:
            inverse = "none: approval required"
        print(
            f"  {spec.name:<24} "
            f"{'W' if spec.writes else 'r':>1} "
            f"{str(spec.idempotent):>4} "
            f"{spec.max_result_tokens:>5}  "
            f"{spec.version:<3}  {inverse}"
        )
    print(f"  read-only set       : {library.read_only_names()}")
    print(f"  writes              : {library.write_names()}")
    print("    ^ read-only is a property of the registered set, which a")
    print("      policy engine can enforce and an auditor can check.")

    banner("the contract issue_refund declares")
    print(f"  version             : {ISSUE_REFUND.version}")
    print(f"  max_result_tokens   : {ISSUE_REFUND.max_result_tokens}")
    print(f"  writes / idempotent : {ISSUE_REFUND.writes} / "
          f"{ISSUE_REFUND.idempotent}")
    print(f"  required arguments  : {ISSUE_REFUND.input_schema['required']}")
    print(f"  output fields       : "
          f"{sorted(ISSUE_REFUND.output_schema['properties'])}")
    print(f"  reason enum         : "
          f"{ISSUE_REFUND.input_schema['properties']['reason']['enum']}")
    print(f"  amount bound        : "
          f"{ISSUE_REFUND.input_schema['properties']['amount_cents']}")
    print("    ^ a schema that rejects a 900,000-cent refund is a control.")
    print("      A description that mentions the limit is a suggestion.")

    if ISSUE_REFUND.max_result_tokens != 200:
        failures.append("issue_refund's result budget is not 200 tokens")
    if ISSUE_REFUND.version != "3":
        failures.append("issue_refund is not at version 3")
    if sorted(ISSUE_REFUND.output_schema["properties"]) != [
        "amount_cents",
        "receipt_id",
        "status",
    ]:
        failures.append(
            "issue_refund's output is not receipt id, amount, and status"
        )
    if "preview_refund" not in library.read_only_names():
        failures.append("preview_refund is not registered as a read")
    if "dry_run" in ISSUE_REFUND.input_schema["properties"]:
        failures.append("issue_refund takes a dry_run flag")
    return library


def show_conformance(library: Any, failures: list[str]) -> None:
    """The suite, then four contracts it refuses."""
    banner("conformance")
    for name, problems in library.registry.report():
        status = "ok" if not problems else "; ".join(problems)
        print(f"  {name:<24} {status}")
        if problems:
            failures.append(f"{name} is registered but fails conformance")
    between = library.registry.check_library()
    print(f"  between tools           {'ok' if not between else between}")
    if between:
        failures.append(f"library-level conformance failed: {between}")

    banner("what registration refuses")
    for label, spec, fn in _bad_contracts():
        try:
            ConformingRegistry().register(spec, fn)
        except ConformanceError as exc:
            first = str(exc).splitlines()[1].strip(" -")
            print(f"  {label:<34}: refused -- {first}")
        else:
            failures.append(f"{label} was registered")
            print(f"  {label:<34}: REGISTERED, which is the defect")

    banner("the overlap test")
    twin = ToolSpec(
        name="find_orders",
        description=SEARCH_ORDERS.description,
        input_schema=SEARCH_ORDERS.input_schema,
        output_schema=SEARCH_ORDERS.output_schema,
        writes=False,
        idempotent=True,
    )
    overlaps = check_library([SEARCH_ORDERS, twin])
    print(f"  search_orders vs find_orders: {overlaps[0] if overlaps else 'ok'}")
    if not overlaps:
        failures.append("the overlap test missed two identical descriptions")
    print(f"  description budget    : {DESCRIPTION_BUDGET} chars, and the")
    print("    longest in this library is "
          f"{max(len(s.description) for s in library.specs())}")


def _bad_contracts() -> list[tuple[str, ToolSpec, Any]]:
    """Four contracts that must not be registrable."""

    def anything(**_: Any) -> dict[str, Any]:
        return {}

    def sql(query: str) -> dict[str, Any]:
        return {"rows": []}

    def refund(order_id: str, amount_cents: int) -> dict[str, Any]:
        return {}

    base_input = {
        "type": "object",
        "properties": {"order_id": {"type": "string"}},
        "required": ["order_id"],
        "additionalProperties": False,
    }
    base_output = {
        "type": "object",
        "properties": {"ok": {"type": "boolean"}},
        "additionalProperties": False,
    }
    return [
        (
            "an idempotent write with no key",
            ToolSpec(
                name="issue_credit",
                description=(
                    "Issue a credit. Use this when a refund is not possible. "
                    "Returns the credit id. Does not move money."
                ),
                input_schema=base_input,
                output_schema=base_output,
                writes=True,
                idempotent=True,
            ),
            anything,
        ),
        (
            "a broad capability",
            ToolSpec(
                name="run_sql",
                description=(
                    "Run a query. Use this when you need data. Returns rows. "
                    "Does not write."
                ),
                input_schema={
                    "type": "object",
                    "properties": {"query": {"type": "string"}},
                    "required": ["query"],
                    "additionalProperties": False,
                },
                output_schema=base_output,
                writes=False,
                idempotent=True,
            ),
            sql,
        ),
        (
            "a description with no 'Does not'",
            ToolSpec(
                name="close_case",
                description=(
                    "Close the case. Use this when it is resolved. Returns "
                    "the case id."
                ),
                input_schema=base_input,
                output_schema=base_output,
                writes=False,
                idempotent=True,
            ),
            anything,
        ),
        (
            "a write with no inverse and no approval",
            ToolSpec(
                name="delete_order",
                description=(
                    "Delete an order permanently. Use this when legal "
                    "requires erasure. Returns the deleted id. Does not "
                    "refund."
                ),
                input_schema=base_input,
                output_schema=base_output,
                writes=True,
                idempotent=False,
            ),
            anything,
        ),
        (
            "an implementation the schema cannot call",
            ToolSpec(
                name="issue_credit",
                description=(
                    "Issue a credit. Use this when a refund is not possible. "
                    "Returns the credit id. Does not move money."
                ),
                input_schema={
                    "type": "object",
                    "properties": {"order_id": {"type": "string"}},
                    "required": ["order_id"],
                    "additionalProperties": False,
                },
                output_schema=base_output,
                writes=False,
                idempotent=True,
            ),
            refund,
        ),
    ]


# ------------------------------------------------------- 2. token accounting


def show_token_accounting(failures: list[str]) -> None:
    """Before and after shaping, on the same data."""
    banner("token accounting: search_orders, shaped and unshaped")
    world = bloated_world()
    unshaped = unshaped_search_orders_of(world)
    raw = unshaped(customer_id="CUST-8841")
    raw_tokens = count_tokens(raw)
    shaped = shape(raw, SEARCH_ORDERS.output_schema)
    result = enforce_budget(SEARCH_ORDERS, {**raw, "call_id": "c1"})
    cap = SEARCH_ORDERS.max_result_tokens

    print(f"  orders in the store   : {len(world.orders)}")
    print(f"  unshaped result       : {raw_tokens} tokens")
    print(f"  after shaping only    : {count_tokens(shaped)} tokens")
    print(f"  after shape + fit     : {count_tokens(result.content)} tokens "
          f"(cap {cap})")
    print(f"  truncated flag        : {result.truncated}")
    print(f"  rows kept             : "
          f"{len(result.content.get('results', []))} of "
          f"{raw['total_matches']}")
    print(f"  cursor                : {result.content.get('cursor')!r}")
    print(f"  note                  : {result.content.get('note')}")
    print("    ^ shape first, then truncate, and always say so. A partial")
    print("      list that does not announce itself is worse than an error:")
    print("      the agent concludes the missing record does not exist.")

    if raw_tokens <= cap:
        failures.append(
            f"the unshaped search only reached {raw_tokens} tokens; the "
            "fixture is too small to demonstrate anything"
        )
    if count_tokens(result.content) > cap:
        failures.append("enforce_budget returned a result over its own cap")
    if not result.truncated:
        failures.append("a truncated result was not flagged")


def show_lint(failures: list[str]) -> None:
    """The lint over the shaped library, then over the unshaped one."""
    banner("the lint, over the shaped library")
    good = build_library(bloated_world())
    probe = ResultProbe(good.registry, enforce=True)
    lines: list[str] = []
    count = lint(FIXTURES, registry=probe, out=lines.append)
    for case in FIXTURES:
        spec = SPECS[case.call.name]
        measured = count_tokens(probe.dispatch(case.call).content)
        print(f"  {case.call.name:<24} {measured:>5} / {spec.max_result_tokens:<4}"
              f"  {case.label}")
    print(f"  failures              : {count}")
    for line in lines:
        print(f"    {line}")
    if count:
        failures.append(f"the shaped library failed its own lint: {lines}")

    banner("the lint, over the unshaped one")
    bad = build_library(bloated_world(), unshaped_search=True)
    bad_probe = ResultProbe(bad.registry, enforce=False)
    bad_lines: list[str] = []
    bad_count = lint(FIXTURES, registry=bad_probe, out=bad_lines.append)
    for line in bad_lines:
        print(f"    {line}")
    print(f"  failures              : {bad_count}")
    print("    ^ exits non-zero in CI. This is the check that would have")
    print("      caught the opening incident's sibling failure.")
    if bad_count < 2:
        failures.append(
            "the lint did not catch the unshaped search as both oversized "
            "and unflagged"
        )


# --------------------------------------------------- 3. preview, commit, ledger


def show_refund_path(failures: list[str]) -> None:
    """A dry run, a commit, and then the same commit with a timeout."""
    banner("preview: a read that commits nothing")
    world = World()
    ledger = SideEffectLedger()
    preview = preview_refund(ORDER, LAMP_SHADE_CENTS, "damaged", world=world)
    for key, value in preview.items():
        print(f"  {key:<20}: {value}")
    print(f"  refunds in the world: {len(world.refunds)}")
    print(f"  ledger rows         : {len(ledger.rows)}")
    if world.refunds or world.ledger:
        failures.append("a dry run moved money")
    if preview["requires_approval"]:
        failures.append(
            f"a {LAMP_SHADE_CENTS}-cent refund asked for approval below the "
            f"{APPROVAL_THRESHOLD_CENTS}-cent threshold"
        )

    banner("the error a model can act on")
    try:
        preview_refund(ORDER, ORDER_TOTAL_CENTS + 1, "damaged", world=world)
    except Exception as exc:  # noqa: BLE001 - the message is the point
        print(f"  {exc}")
        if "amount_exceeds_order_total" not in str(exc):
            failures.append("the over-amount error carries no stable code")
        if "Refund at most" not in str(exc):
            failures.append("the over-amount error suggests no next action")
    else:
        failures.append("a refund over the order total was previewed")

    banner("commit, with a derived key")
    library = build_library(world, ledger)
    call = ToolCall(
        "c1",
        "issue_refund",
        {
            "order_id": ORDER,
            "amount_cents": LAMP_SHADE_CENTS,
            "reason": "damaged",
        },
    )
    first = library.registry.dispatch(call, run_id=RUN_ID, step=3)
    again = library.registry.dispatch(call, run_id=RUN_ID, step=3)
    print(f"  receipt             : {first.content}")
    print(f"  on retry            : {again.content}")
    print(f"  derived key         : "
          f"{idempotency_key(run_id=RUN_ID, step_id='3:c1')}")
    print(f"  refund rows         : {len(world.refunds)}")
    print(f"  total refunded      : {world.total_refunded_cents(ORDER)} cents")
    print(f"  result tokens       : {count_tokens(first.content)} "
          f"(cap {ISSUE_REFUND.max_result_tokens})")
    if len(world.refunds) != 1:
        failures.append(f"{len(world.refunds)} refund rows, expected 1")
    if again.content.get("status") != "duplicate":
        failures.append("the retry did not report itself as a duplicate")
    if world.total_refunded_cents(ORDER) != LAMP_SHADE_CENTS:
        failures.append("the wrong amount was refunded")

    banner("the ledger")
    for row in ledger.to_dicts():
        print(f"  {row['tool']:<22} v{row['version']} "
              f"{row['outcome']:<10} {row['args_fingerprint']} "
              f"-> {row['compensation'] or 'no inverse'}")
    print(f"  unresolved intents  : {len(ledger.unresolved())}")
    print(f"  receipts            : {len(ledger.receipts())}")
    if len(ledger.receipts()) != len(world.refunds):
        failures.append(
            "receipts and refunds disagree, which is the invariant the "
            "nightly reconciliation asserts"
        )


def show_timeout(failures: list[str]) -> None:
    """The dangerous failure: the write landed and the caller cannot know."""
    banner("a timeout, and the row reconciliation reads")
    world = World()
    ledger = SideEffectLedger()
    world.inject_fault("issue_refund", kind="timeout")
    library = build_library(world, ledger)
    call = ToolCall(
        "c9",
        "issue_refund",
        {
            "order_id": ORDER,
            "amount_cents": LAMP_SHADE_CENTS,
            "reason": "damaged",
        },
    )
    result = library.registry.dispatch(call, run_id=RUN_ID, step=7)
    print(f"  the call returned   : ok={result.ok}, "
          f"retryable={result.retryable}")
    print(f"  error               : {result.error}")
    print(f"  the write landed    : {len(world.refunds) == 1}")
    for row in ledger.to_dicts():
        print(f"  ledger row          : {row['tool']} -> {row['outcome']}")
    print(f"  unresolved intents  : {len(ledger.unresolved())}")
    print(f"  total refunded      : {world.total_refunded_cents(ORDER)} cents")
    print("    ^ the intent was written before the call, so reconciliation")
    print("      can tell 'we tried and do not know' from 'we never tried'.")

    if not ledger.unresolved():
        failures.append("a timeout left no unresolved intent to reconcile")
    if ledger.unresolved() and ledger.unresolved()[0].outcome != "unknown":
        failures.append("the timed-out intent is not marked unknown")
    if world.total_refunded_cents(ORDER) != LAMP_SHADE_CENTS:
        failures.append("the timeout did not commit exactly one refund")

    retry = library.registry.dispatch(call, run_id=RUN_ID, step=7)
    print(f"  retry with the same key: {retry.content}")
    print(f"  refund rows after retry: {len(world.refunds)}")
    if len(world.refunds) != 1:
        failures.append("the keyed retry refunded a second time")


def show_compensation(failures: list[str]) -> None:
    """Which actions have an inverse, and which have none at all."""
    banner("compensation, and the set with no inverse")
    for name, compensation in COMPENSATIONS.items():
        print(f"  {name:<24} -> {compensation.tool:<22} "
              f"{compensation.window}")
        print(f"  {'':<24}    costs {compensation.cost}"
              f"{'' if compensation.restores else '; does not restore'}")
    writes = [s.name for s in SPECS.values() if s.writes]
    uncovered = [
        name
        for name in writes
        if name not in COMPENSATIONS and name not in APPROVAL_REQUIRED
    ]
    print(f"  writes                : {writes}")
    print(f"  no inverse, approval  : {sorted(APPROVAL_REQUIRED)}")
    print(f"  uncovered             : {uncovered}")
    print("    ^ empty by construction, not by hope. Actions with neither")
    print("      idempotency nor compensation are exactly the set that must")
    print("      sit behind human approval.")
    if uncovered:
        failures.append(f"writes with no inverse and no approval: {uncovered}")

    world = World()
    ledger = SideEffectLedger()
    library = build_library(world, ledger)
    library.registry.dispatch(
        ToolCall(
            "c1",
            "issue_refund",
            {
                "order_id": ORDER,
                "amount_cents": LAMP_SHADE_CENTS,
                "reason": "damaged",
            },
        ),
        run_id=RUN_ID,
        step=1,
    )
    receipt_id = ledger.receipts()[0]["receipt_id"]
    reversal = cancel_refund(receipt_id, ledger=ledger)
    print(f"  reversing {receipt_id}: {reversal}")
    if ledger.find(ledger.rows[0].key) is None:
        failures.append("the compensation lost its ledger row")
    if ledger.rows[0].outcome != "compensated":
        failures.append("the compensation did not update the ledger")


# ------------------------------------------------------- 4. code execution


def show_sandbox(failures: list[str]) -> None:
    """The contract that lives outside the code."""
    banner("run_code, and the four terms")
    sandbox = NullSandbox(SandboxContract())
    for key, value in sandbox.contract.to_dict().items():
        print(f"  {key:<20}: {value}")

    result = sandbox.run(
        "total = sum(inputs['cents'])\nprint('total_cents', total)",
        {"cents": [5150, 3250]},
    )
    print(f"  stdout              : {result['stdout'].strip()!r}")
    print(f"  wall_seconds        : {result['wall_seconds']}")
    print("    ^ 8,400 cents aggregated without any of the rows entering")
    print("      the context window, which is the saving code execution buys.")

    banner("what the sandbox refuses")
    hostile = [
        ("reaching the network", "import socket\nsocket.socket()"),
        ("reading the filesystem", "print(open('/etc/passwd').read())"),
        ("importing anything at all", "import os\nprint(os.environ)"),
        ("calling another tool", "print(issue_refund('x', 1, 'damaged'))"),
    ]
    for label, program in hostile:
        try:
            sandbox.run(program)
        except SandboxDenied as exc:
            print(f"  {label:<26}: refused -- {exc}")
        else:
            failures.append(f"the sandbox allowed {label}")
            print(f"  {label:<26}: ALLOWED, which is the defect")

    banner("a contract that will not start")
    for label, contract in [
        (
            "a credential in the environment",
            SandboxContract(environment={"DATABASE_URL": "postgres://..."}),
        ),
        ("egress allowed", SandboxContract(egress="allow")),
        ("running as root", SandboxContract(user="root")),
        ("an unpinned image", SandboxContract(image="python:latest")),
    ]:
        try:
            NullSandbox(contract)
        except SandboxDenied as exc:
            print(f"  {label:<32}: refused -- {str(exc)[:96]}")
        else:
            failures.append(f"the sandbox started with {label}")
            print(f"  {label:<32}: STARTED, which is the defect")

    print("\n  What a null sandbox cannot do: preempt. A program that loops")
    print("  forever runs forever, and max_wall_seconds here is measured")
    print("  rather than enforced. That gap is Chapter 12's subject.")


# ------------------------------------------------- 5. the trajectory gate


def show_trajectory_gate(failures: list[str]) -> None:
    """The four-line diff, and the only gate that catches it."""
    banner("the golden trajectory")
    good_world = World()
    good = build_library(good_world)
    good_model = ReadsTheDescription()
    good_run = AgentLoop(
        model=good_model, tools=good.registry, max_turns=8
    ).run(DAMAGED_TICKET, run_id=RUN_ID)
    good_path = path_of(good_run)
    good_traj = trajectory_gate().grade(good_run, good_world)
    good_outcome = outcome_gate().grade(good_run, good_world)
    print(f"  description read as : {good_model.read_description_as}")
    print(f"  trajectory          : {good_path}")
    print(f"  trajectory gate     : {'pass' if good_traj.passed else 'FAIL'}")
    print(f"  outcome gate        : "
          f"{'pass' if good_outcome.passed else 'FAIL'}")
    print(f"  refunded            : "
          f"{good_world.total_refunded_cents(ORDER)} cents")
    if not good_traj.passed or not good_outcome.passed:
        failures.append(
            f"the correct run failed its own gates: "
            f"{good_traj.reasons + good_outcome.reasons}"
        )
    if good_world.total_refunded_cents(ORDER) != LAMP_SHADE_CENTS:
        failures.append("the correct run refunded the wrong amount")

    banner("the four-line diff, labelled documentation only")
    print("  - 'Search orders. Returns a paginated list of matching order ids")
    print("  -  with status and total. Call get_order for the full record.'")
    print("  + 'Search orders. Returns matching orders with status and")
    print("  +  totals.'")
    bad_world = World()
    bad = build_library(
        bad_world, search_description=SEARCH_ORDERS_DRIFTED
    )
    bad_model = ReadsTheDescription()
    bad_run = AgentLoop(
        model=bad_model, tools=bad.registry, max_turns=8
    ).run(DAMAGED_TICKET, run_id=RUN_ID)
    bad_path = path_of(bad_run)
    bad_traj = trajectory_gate().grade(bad_run, bad_world)
    bad_outcome = outcome_gate().grade(bad_run, bad_world)
    print(f"  description read as : {bad_model.read_description_as}")
    print(f"  trajectory          : {bad_path}")
    print(f"  trajectory gate     : "
          f"{'pass' if bad_traj.passed else 'FAIL, as it must'}")
    print(f"  why                 : {bad_traj.reasons[0]}")
    print(f"  outcome gate        : "
          f"{'pass' if bad_outcome.passed else 'FAIL, as it must'}")
    print(f"  refunded            : "
          f"{bad_world.total_refunded_cents(ORDER)} cents, on a claim for a "
          f"{LAMP_SHADE_CENTS}-cent item")
    drifted_spec = bad.registry.spec_for("search_orders")
    unchanged = (
        drifted_spec is not None
        and drifted_spec.input_schema == SEARCH_ORDERS.input_schema
        and drifted_spec.output_schema == SEARCH_ORDERS.output_schema
    )
    print(f"  schemas unchanged   : {unchanged}")
    print("    ^ no schema changed, no argument was malformed, and every")
    print("      unit test on search_orders still passes. A description")
    print("      edit is a behaviour change, so it belongs in the eval gate.")

    if bad_traj.passed:
        failures.append("the trajectory gate passed on the drifted description")
    if bad_outcome.passed:
        failures.append("the outcome gate passed on a whole-order refund")
    if bad_world.total_refunded_cents(ORDER) != ORDER_TOTAL_CENTS:
        failures.append("the drifted run did not reproduce the overpayment")
    overpaid = bad_world.total_refunded_cents(ORDER) - LAMP_SHADE_CENTS
    print(f"  overpayment         : {overpaid} cents on one ticket")


def show_versioning(failures: list[str]) -> None:
    """Two versions side by side, and what a run binds to."""
    banner("versions run side by side, then drain")
    world = World()
    library = build_library(world)
    v3 = library.registry.spec_for("issue_refund")
    print(f"  registered          : issue_refund v{v3.version if v3 else '?'}")
    print(f"  pinned at admission : v{v3.version if v3 else '?'} for this run")
    ledger = library.ledger
    library.registry.dispatch(
        ToolCall(
            "c1",
            "issue_refund",
            {
                "order_id": ORDER,
                "amount_cents": LAMP_SHADE_CENTS,
                "reason": "damaged",
            },
        ),
        run_id=RUN_ID,
        step=1,
    )
    print(f"  ledger records      : v{ledger.rows[0].version}")
    print("    ^ the version is on the row, so a resumed run and an auditor")
    print("      both know which contract the money moved under.")
    if ledger.rows[0].version != "3":
        failures.append("the ledger did not record the pinned tool version")

    narrower = {
        **ISSUE_REFUND.input_schema,
        "properties": {
            **ISSUE_REFUND.input_schema["properties"],
            "reason": {"type": "string", "enum": ["damaged"]},
        },
    }
    print(f"  narrowing the enum  : breaking "
          f"({len(narrower['properties']['reason']['enum'])} values, was "
          f"{len(ISSUE_REFUND.input_schema['properties']['reason']['enum'])})")
    print("  widening an output  : additive")
    print("  editing a descript. : technically compatible, behaviourally")
    print("                        breaking, which is why it is in the")
    print("                        version and in the eval gate")


# ---------------------------------------------------------------------- main


def main(argv: list[str] | None = None) -> int:
    """Run the demo and return a process exit code."""
    _ = list(sys.argv[1:] if argv is None else argv)
    print("Chapter 11 -- a token-budgeted tool library, and the lint")
    print(f"order {ORDER}, {ORDER_TOTAL_CENTS} cents, lamp shade "
          f"{LAMP_SHADE_CENTS}, threshold {APPROVAL_THRESHOLD_CENTS}")

    failures: list[str] = []
    library = show_library(failures)
    show_conformance(library, failures)
    show_token_accounting(failures)
    show_lint(failures)
    show_refund_path(failures)
    show_timeout(failures)
    show_compensation(failures)
    show_sandbox(failures)
    show_trajectory_gate(failures)
    show_versioning(failures)

    print("\n--- what this proves ---")
    print("Tool quality is mechanically checkable. A contract carrying its")
    print("own result budget, write flag, and compensation rule turns three")
    print("classes of production incident into build failures, and a")
    print("description edit is a behaviour change that a trajectory test")
    print("catches and a schema test never will.")

    if failures:
        print("\nFAILED:")
        for line in failures:
            print(f"  - {line}")
        return 1
    print("\nOK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
