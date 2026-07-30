# Chapter 23 — a custom resource, and the same agent at the edge

**What this artifact proves:** an agent's decision logic is genuinely
independent of its deployment shape. The same `build_support_agent` runs under a
Kubernetes controller and inside a hibernating per-session edge object, and the
two leave byte-identical worlds. The operational differences are about
scheduling, state locality, and idle cost — not about the agent.

## Run it

```bash
make demo-ch23
# or
python artifacts/ch23-k8s-edge/demo.py

make kind-up         # the real cluster; needs Docker and kubectl
```

The demo admits and reconciles the `Agent` resource, prints the four objects the
controller writes, reproduces version pinning by editing the resource mid-run,
then runs the same builder inside an edge object that hibernates after the
refund commits and wakes on a fresh shell over the same storage.

It exits non-zero if admission lets a widened egress or a floating model
snapshot through, if reconciliation is not idempotent, if the network policy's
selector drifts from the workload's labels, if an in-flight run moves to a
version it did not start on, if the woken session repeats the refund, or if the
two deployment shapes leave different worlds.

### One honest deviation, stated rather than papered over

The chapter says `make kind-up && make demo-ch23-k8s` brings up a local cluster.
**This demo starts no cluster and runs no `kubectl`.** No chapter demo in this
repository may require a daemon, and CI has neither Docker nor a kube context.
So the manifests ship as real files validated by parsing *and by the same
admission checks the controller applies*, and the controller reconciles against
an in-process API server that is a dict. `make kind-up` is the command that
needs a cluster.

Parsing plus admission is weaker than `kubectl apply --dry-run=server` and it is
not a token check. It rejects an `Agent` that omits its policy reference, floats
its model snapshot, references a tool with no MCP server, or asks for anything
other than deny-by-default egress — which is the failure a controller would
otherwise discover in production.

## Files

| File | What it is |
|---|---|
| `k8s/agent.yaml` | The `Agent` custom resource: version, pinned model snapshot, budget, tool references, policy reference, `egress: deny-by-default`. |
| `k8s/crd.yaml` | The definition, whose `required` list is asserted to match the controller's admission checks. |
| `k8s/networkpolicy.yaml` | What the controller derives from `spec.egress`, written out so a reviewer can read it. |
| `k8s/kind.yaml` | The local cluster: three worker pools, because agents, sandboxes, and GPU inference do not share a scaling signal. |
| `manifests.py` | The YAML subset loader (multi-document, block sequences of mappings), `AgentSpec`, and `admission_problems()`. |
| `controller.py` | The reconciler. One `Agent` in; a Deployment, a NetworkPolicy, a ConfigMap, and a status out. Plus run-version pinning and drain. |
| `agent_builder.py` | `build_support_agent`. The same builder both deployment shapes call, and the whole portability claim. |
| `edge/storage.py` | `LocalStore` (per-session SQL, local to the object) and `StorageCheckpointer` — twelve lines, and the entire storage-specific surface. |
| `edge/session.py` | `SupportSession`, the hibernation hook, and `hibernate_and_wake()`. |
| `demo.py` | All of it, with a non-zero exit on any property failing. |
| `test_ch23.py` | Twenty-five assertions, including the two that protect the portability claim. |
| `conftest.py` | Module isolation when all of `artifacts/` runs under one pytest. |

## Read `agent_builder.py` first, then `edge/session.py`

`build_support_agent` takes a `world` and a `checkpointer`. The checkpointer is
the **only** argument that differs between the two deployment shapes:

```python
# in the worker pod
build_support_agent(world, checkpointer=MemoryCheckpointer(), run_id=run_id)

# in the edge object
build_support_agent(world, checkpointer=StorageCheckpointer(storage),
                    run_id=session_id)
```

`test_both_deployment_shapes_call_the_same_builder` asserts the identity of the
function object, and `test_both_deployment_shapes_leave_the_same_world` asserts
the two produce identical `World.snapshot()` output. If someone forks the
builder to make one platform easier, both fail.

## Three places the code deviates from the obvious

**The world is not session state, and the demo is careful about it.** The
refund service lives *outside* the edge object and outlives every hibernation;
the object's local storage holds where the *run* got to. `hibernate_and_wake()`
therefore builds a brand-new `SupportSession` for the second message — the
object's memory really is gone — while passing the same `LocalStore` and the
same `World`. Conflating the two would let a woken session refund the customer a
second time and report that state had survived, which is the exact class of lie
this book is about.

**Hibernation happens at a step boundary, not inside one.** `Hibernated` is
raised by a step hook the runtime calls *after* the step is durably
checkpointed. Sleeping mid-write leaves the same ambiguity a timeout does, and
the point of a hibernation is to remove ambiguity rather than add it. The demo
sleeps at step 3, which is the step the refund lands on — the only interesting
place to fall asleep.

**The chapter's excerpt calls `self.loop.resume_or_start(text)`; the code puts
that method on the session.** The runtime contract exposes `start` and `resume`
separately, so the shell owns the join. Three branches, and the last is the one
that matters: a resumed session picks up the checkpoint it wrote before it
slept, rather than starting a second run.

## What the controller does not do

It reconciles into four objects and stops. It does not implement leader
election, finalizers, owner references, garbage collection, CRD schema
migration, or the reconcile-on-watch loop — all of which a real controller
needs, and all of which are the operational burden the chapter says you inherit
when you choose this path. The two hundred lines here are the pattern, honestly,
including its failure mode; they are not a controller you should run.
