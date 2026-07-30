# Chapter 17 — spans that say who spent the money, and which dollars moved

**What this artifact proves:** from the emitted spans alone you can say which
agent version, on whose behalf, under what remaining budget, spent how much and
moved which specific dollars — and you can watch the one instrumentation defect
that makes that reconstruction impossible while leaving every dashboard green.
Break the trace edge across the escalation and the total spend is identical, a
fifth of it now belongs to nobody, and trace completeness falls from 1.00 to
0.75.

The backend is one environment variable. Nothing here needs OpenTelemetry
installed, a collector running, or a network.

## Run it

```bash
make demo-ch17
# or
python artifacts/ch17-observability/demo.py
python artifacts/ch17-observability/demo.py --spans     # every span, in order

# same code, a real backend, no application change:
docker compose -f artifacts/ch17-observability/compose.yaml up -d
pip install -e ".[otel]"
NORTHSTAR_OTEL_EXPORTER=otlp \
OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4318 \
  python artifacts/ch17-observability/demo.py
```

Six sections: the one call that instruments a loop; the span tree of a
multi-agent run with the seven incident attributes on a write span; the
three-bucket redaction policy with proof that the customer's message body never
reaches an exporter; cost per run and then cost per verified success; the same
suite with context propagation broken; and trace completeness as an SLI with a
floor that pages someone.

The demo exits non-zero if completeness falls under the floor with propagation
intact, if any write span is missing its side-effect identifier, if breaking the
edge changes the total spend — the whole point of the April story is that the
money was all still there — or if the broken configuration fails to lose the
attribution it exists to lose.

## Files

| File | What it is |
|---|---|
| `spans.py` | `SEMCONV_VERSION`, the `CONVENTION` mapping layer, `SPAN_NAMES`, `RunContext` with its `child()` and `orphan()` constructors, the attribute builders, and `missing_required()`. The three practices that contain an unstable convention live here: pin the version, emit through one mapping, namespace your own. |
| `instrument.py` | `instrument(loop, ctx)` — the whole wiring. `NorthstarInstrumentation` turns the loop's events into the five-level tree; `TracedTools` captures the receipt at the action boundary; `SideEffectIndex` holds it until the tool span closes. |
| `redaction.py` | `Redactor` with three explicit buckets — drop, hash, keep — and `REDACTOR`, Northstar's policy as one reviewable object. Extends the package's redactor rather than forking it. |
| `cost.py` | Trace-linked `CostEvent`s keyed by `(run_id, span_id)`, `Price` with a separate cached-input rate, `cached_split()`, `unattributed_cents()`, and `cost_per_success()`. Accumulates in integer nanocents and rounds once, at the edge. |
| `tickets.py` | The four tickets, one of which escalates, and `run_suite(propagate=...)`. Grading reads each ticket's world, never the run's status field. |
| `otel-collector.yaml` | The tail-sampling policy: keep every run that wrote, was denied, needed a human, failed, or died near its budget; sample the successful read-only runs, which are the volume. |
| `compose.yaml` | An OTLP collector and Arize Phoenix, for when you want the tree in a UI. Optional, and the only thing in the chapter that needs a network — to pull two images. |
| `demo.py` | Both configurations, with every property asserted. |
| `test_ch17.py` | The same properties as assertions on spans and on the ledger, never on a rendered string. |
| `conftest.py` | Path handling, the two suite fixtures, and `unset_exporter`, so the test that asserts the offline default cannot inherit a developer's shell. |

## Read `tickets.py`'s `FRAUD_REVIEW` first, then `RunReport.complete`

The escalated ticket ends like this: the support run reports `succeeded`, the
specialist run reports `succeeded`, the fraud-review case is still open, and
nothing was refunded. Every status field is green and the customer has not been
helped. `_verified()` grades it against the world and returns `False`, which is
the only reason cost per success says anything cost per run does not.

`RunReport.complete` is the SLI, and its second condition is the one Northstar
failed:

```python
has_shape and len(self.trace_ids) == 1 and not self.missing_attributes
```

No attribute is missing in the broken configuration. Every span validates on its
own. The tree is simply two trees, and that is worse than a missing field,
because nothing about a single span tells you the edge is gone.

## Three places this deviates from the chapter's excerpts

**`instrument()` takes a `RunContext`.** The chapter's excerpt is
`instrument(loop, exporter=exporter)`, and that call exists — it is
`northstar_telemetry.instrument`, and this module calls it to choose the
backend, because that function is where the with- and without-OpenTelemetry
branch lives. What it cannot supply is the run's identity: the goal, the agent
version, the config hash, the principal, and the budget have no standardized
attribute name and no way to be discovered from a loop. So they arrive as a
context object, and the module then replaces the sink the package attached.

**The side-effect id is captured by the tool registry, not read off an event.**
`northstar.side_effect.id` does not exist until the tool returns, and the loop's
`tool.result` event carries whether the call succeeded without carrying the
receipt. `TracedTools.dispatch` records it at the action boundary — the only
place it is available. Without that, every write span in the trace has a hole
exactly where the join key to the ledger belongs, which is the attribute that
would have made Chapter 1's double refund a five-minute investigation.

**Five of the seven are required everywhere; two are required on tool spans.**
The chapter says all seven on every span a human might read. An argument digest
on a model span would be a field with nothing in it, and a required field that
is routinely empty is a required field nobody believes — so `required_for()`
splits them, and `missing_required()` takes the span name. The strict reading is
still available by omitting it.

## What the numbers are, and are not

`NORTHSTAR_PRICES` is an illustrative placeholder, dated `2026-07-27`, and it is
not a claim about any provider. Every figure the demo prints is computed from
runs that happened: token counts come from `FakeModel`'s deterministic estimate,
the cached split is computed rather than declared, and the pricing version rides
on every event so a rate-card change cannot rewrite last month.

The absolute cents are small — a mock-mode run costs a fraction of a cent — so
the tables print unrounded cents. That is a display concern; `per_run_cents()`
still rounds up once at the edge, because nothing should ever round a bill down.
Cost per success is dominated by the twelve specialist minutes the escalated
ticket consumed, and that is not an artifact of the mock: an agent that is cheap
in tokens because it escalates constantly has moved cost rather than removed it.
