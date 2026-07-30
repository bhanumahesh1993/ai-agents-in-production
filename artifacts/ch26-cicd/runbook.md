# Incident runbook — Northstar Returns support agent

> Template. Copy it, fill in the owners and the links, and keep it where the
> pager points. A runbook nobody has opened is a document, not a control.

| Field | Value |
|---|---|
| Agent | `northstar-support-agent` |
| Owner (team) | *fill in* |
| Owner (on-call rotation) | *fill in* |
| Escalation after 30 minutes | *fill in* |
| Dashboards | *fill in* |
| Kill-switch console | *fill in* |
| Side-effect ledger | *fill in* |
| Last drill | *date, and who ran it* |

---

## The five first checks

Always these, always in this order. Answer them before you form a theory.

**1. Which configuration hash is running?**
Every run carries an effective hash over code, model snapshot, prompt, tool
versions, policy, guardrails, and sandbox image. Read it off any affected run,
then diff it against the previous one and list what changed. If this question
takes more than a minute, that is a second finding.

```
python -m gates.reliability --version <tag> --json | jq .version
```

**2. What is the blast radius?**
Count affected runs, users, tenants, and **mutations** — from the side-effect
ledger, not from the trace. A trace says what the agent believed. The ledger
says what happened.

**3. Is it still happening?**
If yes, contain before you diagnose. Admission off or mutations off is cheaper
than being right. See the containment ladder below.

**4. Which class of incident is this?**
Wrong goal · wrong tool · wrong arguments · wrong authority · partial commit ·
duplicate commit · memory contamination · runaway loop · silent failure ·
unsafe success · supply-chain change · telemetry loss.

Naming the class narrows both the containment action and the fix, and it is
what turns a pile of incidents into a prioritised list of failure modes.

**5. Can we reconstruct one affected run end to end?**
Goal, plan, every tool call and result, every authorisation decision, every
approval, and the ledger entries. If not, that is a second incident, and it is
the one that will make the first un-closable.

---

## Containment ladder

Independent flags, each smaller and faster than a deploy. Pull the narrowest
one that stops the bleeding.

| Rung | Flag | Stops | Leaves running |
|---|---|---|---|
| 1 | `admit_new_runs` | new runs | **everything already in flight** |
| 2 | `tool:<name>` | one tool, fleet-wide | every other tool |
| 3 | `all_mutations` | every write, including in-flight | reads |
| 4 | `external_egress` | customer-visible messages | internal work |
| 5 | `memory_writes` | memory contamination spreading | the current run |
| 6 | `agent_version:<tag>` | one version's admissions | other versions |

**Rung 1 is not containment.** It stops new work. Runs already halfway through
a trajectory keep acting. That is the failure `drill.py` exists to demonstrate,
and it is why the flags are checked at the action boundary on every dispatch
rather than once at admission.

---

## Response sequence, once contained

```text
detect
  → classify side effects and blast radius
  → disable admission or mutation tools
  → revoke credentials and delegations
  → stop active sessions and their children
  → preserve traces, checkpoints, memory revisions, and the ledger
  → reconcile authoritative external state
  → compensate and recover customers
  → identify configuration and supply-chain versions
  → add regression and adversarial evals
  → fix, shadow, canary, restore
```

The order is deliberate. **Preservation comes before reconciliation**, because
reconciliation mutates the evidence. **Credential revocation comes early**,
because if the cause is a compromised delegation, everything after it is theatre
until the credential is dead.

---

## Recovery semantics, decided in advance

Written next to the tool, not decided under pressure. This classification
determines the approval requirement and the promise you can honestly make.

| Tool | Class | Recovery |
|---|---|---|
| `get_order`, `get_policy`, `search_orders` | read-only | nothing to recover |
| `issue_refund` | compensatable, **not** reversible | clawback, with customer contact; reconcile against the ledger |
| `send_message` | irreversible | the customer has read it; correct by sending a second message, never by pretending |
| `escalate_to_specialist` | human-reversible | close the case with a note |

A refund is compensatable. An email is irreversible. Treating the two
identically is how a runbook step becomes a wrong instruction at 03:12.

---

## Incident record

Capture all of it. This is the artifact that does double duty for debugging,
for the postmortem, and for whatever Volume 2's control framework asks of you
later.

- [ ] First and last occurrence
- [ ] Agent, config hash, model snapshot, tool versions
- [ ] Affected users, tenants, orders, and resources
- [ ] Goals, plans, and trajectories
- [ ] Authorisation, delegation, and approval decisions
- [ ] Memory and retrieval inputs
- [ ] The side-effect ledger extract
- [ ] External transaction identifiers
- [ ] Containment and kill-switch actions taken, with times
- [ ] Customer remediation
- [ ] Root cause and contributing conditions
- [ ] New regression tests and evaluation cases added
- [ ] Owner, with a follow-up deadline

---

## Closing the postmortem

Two rules.

**Classify against the taxonomy** rather than writing a unique narrative. A
folder of narratives cannot be sorted; a column of classes can.

**The postmortem is not complete until a test exists.** The specific scenario
becomes a case in the evaluation dataset, and the failure mode becomes a
detector. An incident that produces only a document is scheduled to recur, and
it will recur in a way that looks new.

---

## Drill schedule

Quarterly is a reasonable floor. Run `python artifacts/ch26-cicd/demo.py` — its
drill section pulls every rung and asserts what each one achieved. Record the
date and the result in the header table above. The failure a drill finds is
almost always that containment stopped new work and left in-flight runs
mutating.
