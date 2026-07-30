# Chapter 11 — a token-budgeted tool library, and the lint that fails the build

**What this artifact proves:** tool quality is mechanically checkable. A contract
that carries its own result budget, write flag, and compensation rule turns
three classes of production incident into build failures — an oversized result,
a claimed idempotency the target system does not provide, and an irreversible
unattended write — and a description edit is a behaviour change that a
trajectory test catches and a schema test never will.

## Run it

```bash
make demo-ch11
# or
python artifacts/ch11-tools/demo.py
pytest -q artifacts/ch11-tools
```

The demo registers the eight tools through the conformance gate, then walks
everything the chapter argues for:

1. **The library, as contracts.** Write flag, idempotency, result cap, version,
   and declared inverse for each of the eight, plus the read-only set as a
   property of the registered tools rather than a promise in a document.
2. **Conformance.** Every registered tool reports clean; five contracts are then
   refused at registration, including an idempotent write with no key, a
   `run_sql`, and a write with neither an inverse nor an approval rule.
3. **Token accounting.** The same 78-order search, unshaped (6,420 tokens),
   shaped (1,871), and shaped-then-fitted (786 against a cap of 800), with the
   cursor and the truncation note it hands back.
4. **The lint.** Zero failures over the wired library. Two failures over the
   unwired one — `6420 tokens > cap 800` and `over cap and not flagged` — which
   is the check that would have caught the opening incident's sibling failure.
5. **Preview, commit, ledger.** A dry run that moves nothing, the error a model
   can act on, a commit with a derived key, the retry that returns the original
   receipt, and the ledger rows.
6. **A timeout.** The write lands, the response never comes back, the intent row
   says `unknown`, and the keyed retry pays once.
7. **Compensation.** Which writes have an inverse, for how long, at what cost,
   and the check that the set of irreversible unattended actions is empty.
8. **`run_code`.** The four contract terms as data, four programs the sandbox
   refuses, and four contracts that will not start at all.
9. **The four-line diff.** The golden trajectory passing its gates, then the
   drifted description producing `['search_orders', 'issue_refund']`, an
   8,400-cent refund on a 3,250-cent claim, and both gates failing — with the
   schemas byte-for-byte unchanged.

It exits non-zero on any conformance failure, any oversized or unflagged result
from the wired library, a lint that misses the unwired one, a timeout that
leaves no unresolved intent, a dry run that moves money, or a trajectory gate
that passes on the drifted description.

## Files

| File | What it is |
|---|---|
| `specs.py` | The eight contracts. `ISSUE_REFUND` with `max_result_tokens=200`, `version="3"`, and a receipt-shaped output; `PREVIEW_REFUND` as its own `writes=False` tool; `REFUND_INPUT` and `REFUND_OUTPUT`; `COMPENSATIONS` and `APPROVAL_REQUIRED`; `BROAD_CAPABILITIES`; and `SEARCH_ORDERS_DRIFTED`, the four-line diff kept as a fixture. |
| `budget.py` | `enforce_budget(spec, payload)` — shape, then truncate, then always declare truncation — with `shape()`, `fit()`, `count_tokens()`, and `CURSOR_TOKENS`. |
| `refund.py` | `preview_refund()`, `issue_refund()`, `cancel_refund()`, and `SideEffectLedger` with `record_intent` / `record_outcome` / `record_unknown`. `RefundPath` binds the world and ledger for registration. `POLICY_REASON` maps the tool's enum onto the policy store's. |
| `sandbox.py` | `SandboxContract` — the four terms as checked data — and `NullSandbox`, which is named for what it is and which Chapter 12 replaces. |
| `conformance.py` | `check(spec, fn)`, the chapter's excerpt, plus `check_library()` for the overlap test and the dry-run rule, and `ConformingRegistry`, whose `register` runs the gate so a non-conformant tool cannot be registered at all. |
| `lint_results.py` | `lint(cases)`, `FixtureCall`, `FIXTURES`, `bloated_world()`, and `ResultProbe`, which measures what the *tool* produced rather than what the runtime's truncation left. |
| `library.py` | `build_library()`: the eight contracts wired to eight implementations, each wrapped in `budgeted()`. Also the shaped and unshaped `search_orders`. |
| `golden.py` | The golden trajectory, `trajectory_gate()`, `outcome_gate()`, and `ReadsTheDescription` — a deterministic stand-in that reads the `search_orders` description and branches on it. |
| `demo.py` | Everything above, printed. |
| `test_ch11.py` | The properties as assertions, starting with the five version-critical facts. |
| `conftest.py` | Path handling and the function-scoped fixtures. |

## Read the first five tests, then `conformance.py`

The tests under "the version-critical facts" are the shortest complete statement
of what an external audit caught here:

```python
assert ISSUE_REFUND.max_result_tokens == 200
assert ISSUE_REFUND.version == "3"
assert sorted(ISSUE_REFUND.output_schema["properties"]) == [
    "amount_cents", "receipt_id", "status",
]
assert PREVIEW_REFUND.writes is False              # a tool, not a flag
assert "dry_run" not in ISSUE_REFUND.input_schema["properties"]
```

Then `conformance.py`, because it is the file worth copying. Two of its rules
carry the chapter. The `writes and idempotent` rule catches the most dangerous
lie a contract can tell. The compensation rule does not require every write to
be reversible; it requires every write to be *either* reversible *or* listed in
the approval policy, so the set of irreversible unattended actions is empty by
construction rather than by hope.

## The three incidents this turns into build failures

1. **An oversized tool result.** `max_result_tokens` is part of the contract, so
   a tool that returns thousands of tokens of JSON fails the lint instead of
   quietly costing you on every subsequent turn of every run — and a truncated
   result that does not set its flag fails a second time, because an agent
   reasoning about a partial list as though it were complete is worse than an
   error.
2. **A claimed idempotency nothing enforces.** `writes=True` plus
   `idempotent=True` requires the key in the schema's `required` list.
   Conformance rejects the pair without it, because retry logic will be built on
   that declaration.
3. **An irreversible unattended write.** Every write must be either reversible
   or listed in the approval policy. `run_code` has no inverse, so it is in
   `APPROVAL_REQUIRED` by construction.

## Where the code goes beyond the chapter's excerpts

**The excerpts are abbreviated; the code is not.** `check()` in the chapter
shows eight rules. The real one also refuses a name from `BROAD_CAPABILITIES`,
an output schema with no properties, an output schema that declares `call_id`, a
non-positive result budget, a missing version, and an implementation whose
signature cannot be called from its own input schema. That last one is why
`check` takes `fn` at all: the registry dispatches `fn(**arguments)`, so a
required field the signature cannot accept is a `TypeError` at the worst
possible moment.

**The printed `ISSUE_REFUND` description would fail its own gate.** The
chapter's excerpt has no "Returns" and no "Use this when", and
`REQUIRED_SECTIONS` demands all three. The real description keeps every sentence
the excerpt prints and adds the two missing sections. Worth naming rather than
quietly fixing: the rule is right and the excerpt is short.

**`issue_refund` takes a `reason` and a world.** The chapter's excerpt shows
`issue_refund(order_id, amount_cents, idempotency_key)` with a module-level
`ledger` and `world`. The real signature takes `reason` — the schema requires it
— and takes the world and ledger as keyword arguments, so one test can build its
own of each and no test can see another's refunds. `RefundPath` binds them for
registration, because a registry calls a tool with the model's arguments and
nothing else.

**Two `reason` vocabularies, mapped rather than conflated.** The chapter's
`issue_refund` enum is `damaged`, `not_received`, `wrong_item`, `changed_mind`;
the policy store in `northstar_contracts` knows `damaged`, `not_delivered`,
`changed_mind`, `fraud_suspected`. Both are correct for their own side, and
`POLICY_REASON` in `refund.py` is the whole translation. Putting it next to the
call rather than in the schema means widening the tool's enum is a schema change
and remapping is a code change.

**`ConformingRegistry` stamps the idempotency key before validating.** The
runtime's order is validate-then-stamp, which is right when the key is optional
— Chapter 1 needs a refund that can be made without one. Every write here
declares the key *required*, because a call without one must not reach the tool
and a model should not be inventing keys. So the stamp happens first, derived
from `(run_id, step, call_id)`, and validation then sees a complete call. A call
dispatched with no `run_id` fails validation, which is the correct outcome.

## What is mocked, and what that costs

**`FakeModel` cannot read a description.** It is scripted per goal, so it cannot
reproduce the opening incident on its own. `ReadsTheDescription` in `golden.py`
is a deterministic stand-in that *does* read the `search_orders` description it
is handed and branches on whether that description still says the rows are
partial and says what to call next. That makes the description causal here,
which is what the gate needs to be worth anything. It is not a claim about how a
real model reads prose. What is demonstrated is that the trajectory gate fires
on the trajectory change; whether a given model makes that change against a
given description is an empirical question only an eval against that model
settles.

**`NullSandbox` cannot preempt.** A program that loops forever runs forever.
`max_wall_seconds` is declared and measured, not enforced, and there is no
memory cap, no filesystem, and no process boundary. What it does provide is the
control that matters most and is also the cheapest: there is no `__import__` in
the execution namespace, so nothing in a program can reach the network, the
filesystem, or another tool. Real isolation — containers, syscall filtering,
microVMs, managed sandboxes — is Chapter 12, and the gap between the two is that
chapter's whole subject.

**The token counts are an approximation.** `count_tokens` delegates to the
repository's four-characters-per-token estimate, which is wrong against a real
tokeniser by roughly 10-20%, consistently. Budgeting needs a number that is
stable, offline, and identical on every machine; billing does not. The 6,420
figure the demo prints is that estimate, which is what makes it reproducible.

**`cancel_refund` is not registered as a tool.** It exists so an approval can
tell a human the action is reversible, and so a supervisor can undo a step
without inventing a procedure. Compensation is an operator capability, not an
agent capability, and giving the model its own undo button is a different design
with a different threat model.
