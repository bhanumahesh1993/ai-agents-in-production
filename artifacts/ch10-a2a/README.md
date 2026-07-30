# Chapter 10 — two agents, two runtimes, one wire contract

**What this artifact proves:** two agents on different runtimes can discover each
other and delegate the fraud-review handoff with no shared code; the trust
decision is a pinned, signed Agent Card rather than a hostname; and a retried
delegation produces one unit of work rather than two — one task, one review, one
hold on the customer's money.

## Run it

```bash
make demo-ch10
# or
python artifacts/ch10-a2a/demo.py
python artifacts/ch10-a2a/demo.py --tamper-card
```

The demo resolves the peer's card, prints what the trust policy checked and what
it refuses, then delegates the review of order `NR-2026-0042110` — 24,000 cents,
shipped, flagged `fraud_review` — and drives the task through
`TASK_STATE_SUBMITTED`, `TASK_STATE_WORKING`, `TASK_STATE_INPUT_REQUIRED` and
`TASK_STATE_COMPLETED`. It then:

1. **Resends the identical delegation** and shows the peer's task store still
   holding exactly one task, with `reviews_opened` unchanged at one.
2. **Runs the other blocking state.** A delegation minted with a weak assurance
   level stops in `TASK_STATE_AUTH_REQUIRED`, which routes to an authorization
   server and asks the customer nothing.
3. **Runs the admission refusals.** An unknown order, an unoffered skill, and an
   incomplete handoff contract each come back as a terminal
   `TASK_STATE_REJECTED` task that ran no domain work.
4. **Shows what travels and what must not.** Tenant, restated constraints,
   remaining budget, provenance, and a scoped grant travel; a forwarded
   credential, a missing tenant, an empty task id, a wrong audience, an expired
   grant, and a grant without the scope are each refused by the receiver.
5. **Walks the lifecycle**, printing every illegal transition it refuses,
   including a lowercase state label used as a wire value.
6. **Runs the same tool inside `AgentLoop`**, so the model calls
   `escalate_to_specialist` exactly as it has since Chapter 1 and cannot tell
   that the implementation now crosses a protocol boundary.

`--tamper-card` runs only the refusal set, which is the form the chapter
describes: five substituted cards, five closed doors. Without it the demo exits
non-zero only when something behaves differently from the above.

## Files

| File | What it is |
|---|---|
| `wire.py` | The A2A v1.0 object model, and the only module both halves import. The eight prefixed `TASK_STATE_*` wire values, `SHORT_LABELS` for prose, `LEGAL_TRANSITIONS`, `advance()`, `require_wire_state()`, `AgentCard`, `Interface`, and `Task`. |
| `cards/fraud-review.json` | The card the peer publishes, byte-for-byte as the chapter prints it. Ordered `supportedInterfaces[]`, and no top-level `url`, `protocolVersion`, or `preferredTransport`. |
| `cards/fraud-review.sig.json` | The detached signature over the card body. |
| `cards/fraud-review.pre-1.0.json` | The porting counter-example. A legacy-shaped card that `AgentCard.from_dict` refuses rather than reads around. |
| `client/pins.json` | Deployed configuration: url, verifying key, approved card hash, required scope, and who reviewed it when. |
| `client/resolve.py` | `resolve_peer(peer_id, registry)`, the chapter's excerpt, with `PeerRegistry`, `Pin`, `verify_signature()`, `sha256_of()`, and `SUPPORTED_A2A_VERSIONS`. Also `skill_description()`, which is the one place third-party text enters the client. |
| `client/escalate.py` | `escalate_to_specialist(order_id, reason, run_id, step_id)`, `build_delegation()` with all six handoff fields, `mint_delegation()`, `RunBudget.remainder()`, `Delegator` (the book's `Principal` plus a tenant), and `escalation_tool()`, which registers the same delegation as the loop's tool. |
| `client/follow.py` | `TERMINAL`, `handle(task, ctx)` — the four-branch state machine — and `drive()`, the poll loop `handle` is the body of. |
| `peer/fraud_review.py` | The fraud review agent as a five-node graph on its own runtime, with its own `World`. Two nodes stop: `check_assurance` in `auth_required`, `need_evidence` in `input_required`. |
| `peer/adapter.py` | The A2A server: authenticate the grant, authorize the skill, resolve and namespace the tenant, validate the handoff, apply a quota, audit. `send_task` is idempotent on `(tenant, task_id)`. |
| `transport.py` | `MockTransport`: card resolution at the well-known path plus the five task operations, in-process. `tamper()`, `serve_card()`, and `serve_legacy_card()` are the hostile conditions. |
| `wiring.py` | `wire_link()`: the one module allowed to import both halves. Keeping the assembly here is what lets a reader confirm by grepping that `client/` never reaches the peer's code. |
| `demo.py` | Everything above, printed, with `--tamper-card`. |
| `test_ch10.py` | The properties as assertions, including every legal and every illegal state transition. |
| `conftest.py` | Path handling and the function-scoped fixtures. |

## Read `test_ch10.py`'s first five tests, then `client/follow.py`

The five tests under "the version defects" are the shortest complete statement of
what an external audit caught here, and each one fails silently in production:

```python
assert "url" not in body                    # v1.0 has no top-level url
assert "protocolVersion" not in body        # nor a top-level version
with pytest.raises(ValueError, match="human label"):
    require_wire_state("completed")         # a label is never a wire value
```

A client comparing against `"completed"` never sees a terminal state, so it polls
finished work until it times out. That is the 09:14 incident reappearing through
a version mismatch instead of through a missing state.

`client/follow.py` is the other file to read. It is four branches and thirteen
lines, and the two extra branches are the entire repair. `input_required` and
`auth_required` **suspend** — the run parks against its checkpoint — and they are
kept apart because they resolve through completely different systems.

## Where the code goes beyond the chapter's excerpts

**The excerpts are abbreviated; the code is not.** `resolve_peer` shows three
refusals. The real one also refuses an unreviewed peer, a card naming a different
agent, a card pointing at an unpinned url, a card offering no interface at a
supported version, a card that dropped the skill, and a card whose declared
scopes no longer include the pinned one. The three the chapter prints are the
three that need the explanation; the rest are the same idea applied to the rest
of the pin.

**The delegation carries nine keys, not eight.** The excerpt shows `task_id`,
`skill`, `tenant`, `goal`, `constraints`, `budget_remaining`, `provenance`, and
`auth`. The real payload also carries `state_ref` and `return_contract`, because
all six fields of Chapter 6's handoff contract have to travel and the receiver
rejects a payload missing any one of them. `test_an_incomplete_handoff_is_rejected`
drops each of the six in turn.

**`protocol_version` is derived, not a field.** `AgentCard.protocol_version` and
`AgentCard.url` are read-only properties over `supportedInterfaces[0]`, so the
chapter's `card.protocol_version` line is true of the code while the wire format
has no such field. `AgentCard.interface_for(binding=..., versions=...)` is what
actually picks an interface, walking the list in the card's own order.

**The task id in the loop's path goes through the registry's stamp.**
`escalate_to_specialist` derives `task_id = idempotency_key(run_id, step_id)`
directly. When the same delegation is registered as a tool, `ToolRegistry`
stamps `idempotency_key(run_id, f"{step}:{call_id}")` and that stamp *is* the
step identifier the task id is then derived from. Both paths are a pure function
of the run and the step, which is the property that matters; the tool path just
gets its step identity from the runtime rather than from the caller.

## Two places this artifact makes a modelling decision the prose leaves open

**`submitted -> completed` is illegal here.** The chapter says `submitted` means
queued and `working` means execution started. A task that completes without ever
working would say the peer finished work it never began, so `LEGAL_TRANSITIONS`
does not allow it, and neither does `submitted -> input_required`: a peer cannot
discover that its inputs are insufficient before reading them. `rejected` is
likewise an *initial* state rather than a move out of `submitted`, because a
refusal at admission means the task was never queued.

**Identity failures raise; payload failures return a rejected task.** No tenant,
no task id, a forwarded credential, a wrong audience, an expired grant, or a
missing scope raise `AdmissionRefused`. A malformed handoff, an unoffered skill,
an unknown order, a missing restated constraint, or a tenant over quota come back
as a terminal `rejected` task. The line is whether the request could be keyed
into a store partitioned by tenant at all: an unauthenticated caller does not get
a task id back to poll.

## What is mocked, and what that costs

The transport is in-process, so the demo is deterministic, needs no credentials,
and cannot be broken by someone else's outage. Two things that buys nothing:

**Conformance.** Both halves of this artifact were written against the same
`wire.py`, so of course they agree. Two independent implementations agreeing on a
specification has never removed the need to test against the actual peer, and
nothing here substitutes for that. `test_the_client_can_be_pointed_at_a_second_peer`
demonstrates the registry plumbing for a second peer and says so in its
docstring: it is the same implementation behind a second url, not a foreign stack.

**Real signatures.** The card is signed with an HMAC over its canonical JSON
rather than a detached JWS, because the repository installs no crypto library and
mock mode has to work with the standard library alone. The `Pin.public_key` field
keeps the name a real deployment would use. The property demonstrated is the one
that matters — a body that changed by one byte stops verifying — but this is not
a signature scheme to copy.

**Streaming and push notifications** are declared in the card's `capabilities`
and are not implemented. The demo polls. Both are transport mechanics on top of
the same task lifecycle, and the lifecycle is what this chapter is about; a push
endpoint is an inbound HTTP endpoint accepting state-changing callbacks from a
remote party, which needs its own authentication, replay protection, and tenant
resolution, and cannot be built offline without pretending.
