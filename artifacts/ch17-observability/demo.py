"""Instrument the Northstar agent, then break one edge and watch cost lose an owner.

    python artifacts/ch17-observability/demo.py
    python artifacts/ch17-observability/demo.py --spans
    NORTHSTAR_OTEL_EXPORTER=otlp python artifacts/ch17-observability/demo.py

Six sections:

1. **One call to instrument.** The exporter comes from one environment
   variable and defaults to ``console``, so this runs with nothing installed.
2. **The span tree.** Run, model, tool, handoff -- with the seven attributes
   that make a trace usable during an incident, and the session as a
   correlation id rather than a root.
3. **Redaction.** The three buckets, and proof that the customer's message
   body is dropped in-process while the amount in cents survives.
4. **Cost per run, then cost per verified success.** With the cached-token
   split, a pricing version on every event, and human minutes counted.
5. **The same suite with context propagation broken.** Northstar's April
   failure: identical total spend, a fifth of it now belonging to nobody.
6. **Trace completeness as an SLI.** 1.00 with the edge, 0.75 without it.

Exits non-zero if trace completeness falls under the floor in the propagated
configuration, if any write span is missing its side-effect identifier, if the
broken configuration fails to lose the attribution it is supposed to lose, or
if breaking the edge changes the total spend -- because the whole point of the
April story is that the money was all still there.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from cost import PRICING_VERSION, cached_split  # noqa: E402
from instrument import (  # noqa: E402
    AGENT_VERSION,
    CONFIG_HASH,
    EXPORTER_ENV,
    SESSION_ID,
    chosen_exporter,
)
from redaction import REDACTOR  # noqa: E402
from spans import (  # noqa: E402
    IDENTITY_ATTRIBUTES,
    REQUIRED_ATTRIBUTES,
    SEMCONV_VERSION,
    SPAN_NAMES,
    TOOL_ATTRIBUTES,
)
from tickets import (  # noqa: E402
    HUMAN_MINUTE_CENTS,
    TICKETS,
    SuiteResult,
    run_suite,
)

#: The floor below which a missing-trace rate pages someone. Trace
#: completeness measures your evidence rather than your service, which is
#: exactly why it earns a place in the SLI catalog.
COMPLETENESS_FLOOR = 0.99


def section(title: str) -> None:
    """Print one section header."""
    print(f"\n=== {title} ===")


class FirstLines:
    """A stream that keeps the first few lines and drops the rest.

    The console exporter writes one line per span, which is the right
    behaviour and the wrong length for a page of a book.
    """

    def __init__(self, limit: int = 6) -> None:
        self.limit = limit
        self.lines: list[str] = []

    def write(self, text: str) -> int:
        """Keep ``text`` if there is room, and report it as written."""
        if len(self.lines) < self.limit and text.strip():
            self.lines.append(text.rstrip("\n"))
        return len(text)

    def flush(self) -> None:
        """Nothing is buffered."""


def show_wiring(used: str) -> None:
    """One call, and a backend that is deployment config."""
    section("instrumentation is one call, and the backend is one variable")
    print(f"  {EXPORTER_ENV:<26} {chosen_exporter()!r} (default 'console')")
    print(f"  {'exporter this demo used':<26} {used!r}")
    print("  'memory' is what lets the tables below be printed at all: the")
    print("  demo reads its own spans back. 'console' writes one line per")
    print("  span, and 'otlp' reads OTEL_EXPORTER_OTLP_ENDPOINT. Same spans,")
    print("  three destinations, no application change.")
    print(f"  {'semconv version':<26} {SEMCONV_VERSION}")
    print(f"  {'agent version':<26} {AGENT_VERSION}")
    print(f"  {'config hash':<26} {CONFIG_HASH}")
    print(f"  {'session (correlation id)':<26} {SESSION_ID}")
    print("  the session is carried on every span and is never a root: a")
    print("  trace spanning a week of conversation is a trace no backend")
    print("  renders and no engineer reads.")

    stream = FirstLines()
    run_suite(propagate=True, exporter="console", stream=stream,
              tickets=TICKETS[:1])
    print("\n  the console exporter, on the first ticket:")
    for line in stream.lines:
        print(f"    {line[:96]}")


def show_tree(suite: SuiteResult, verbose: bool) -> None:
    """The five levels, and the seven attributes."""
    section("the span tree of one multi-agent run")
    escalated = next(r for r in suite.runs if r.escalated)
    counts: dict[str, int] = {}
    for span in escalated.spans:
        counts[span.name] = counts.get(span.name, 0) + 1
    for label, name in SPAN_NAMES.items():
        if name in counts:
            print(f"  {label:<9} {name:<20} x{counts[name]}")
    print(f"  trace ids in the tree: {len(escalated.trace_ids)} "
          f"(one is the healthy answer)")

    tool_span = next(
        s for s in escalated.spans
        if s.name == SPAN_NAMES["tool"]
        and s.attributes.get("northstar.tool.writes")
    )
    print("\n  the seven, on a write span:")
    for key in REQUIRED_ATTRIBUTES:
        print(f"    {key:<38} {tool_span.attributes[key]!r}")
    print(f"  five are required everywhere: {len(IDENTITY_ATTRIBUTES)}")
    print(f"  two describe one call:        {len(TOOL_ATTRIBUTES)}")

    handoff = next(s for s in escalated.spans if s.name == SPAN_NAMES["handoff"])
    print("\n  the handoff, visible as a handoff:")
    for key in sorted(handoff.attributes):
        if key.startswith("northstar.handoff"):
            print(f"    {key:<38} {handoff.attributes[key]!r}")

    if verbose:
        print("\n  every span, in order:")
        for span in escalated.spans:
            print(f"    {span.name:<20} {span.status:<5} "
                  f"{span.attributes.get('gen_ai.tool.name', '')}")


def show_redaction(suite: SuiteResult, failures: list[str]) -> None:
    """Three buckets, decided in code review rather than in a wiki."""
    section("payloads: dropped, hashed, or kept")
    for path in (
        "arguments.body",
        "arguments.order_id",
        "arguments.amount_cents",
        "arguments.unclassified_field",
    ):
        print(f"  {path:<32} -> {REDACTOR.classify(path)}")

    span = next(
        s for s in suite.runs[0].spans
        if s.name == SPAN_NAMES["tool"]
        and s.attributes.get("gen_ai.tool.name", "").startswith("send_message")
    )
    exported = span.attributes["northstar.tool.arguments"]
    print(f"\n  send_message arguments as exported: {exported}")
    if "body" in exported:
        failures.append("the customer's message body reached the exporter")
    if not str(exported.get("order_id", "")).startswith("sha256:"):
        failures.append("the order id was exported readable")


def show_cost(suite: SuiteResult) -> None:
    """The cached split, the pricing version, and the right denominator."""
    section("cost per run, and then cost per verified success")
    print(f"  pricing version {PRICING_VERSION} on every event")
    print(f"  cached_split(4120, 3800) -> {cached_split(4120, 3800)} "
          f"(uncached, cached)")
    print("\n  ticket     run                  status     verified  "
          "cents  traces  complete")
    for row in suite.table():
        print(
            f"  {row['ticket']:<10} {row['run_id']:<20} "
            f"{row['status']:<10} {str(row['verified']):<8}  "
            f"{row['cents']:>6.4f}  {row['traces']:>6}  {row['complete']}"
        )
    tokens = suite.cost.tokens()
    print(f"\n  prompt tokens        {tokens['input_tokens']}")
    print(f"  of which cached      {tokens['cached_input_tokens']} "
          f"(priced separately, or the bill is overstated)")
    print(f"  completion tokens    {tokens['output_tokens']}")
    print(f"  model calls          {tokens['calls']}")
    print(f"\n  model spend          {suite.total_exact_cents():.4f} cents")
    print(f"  cost per run         {suite.cost_per_run():.4f} cents "
          f"(the wrong denominator)")
    print(f"  verified successes   {len(suite.successes)} of {len(suite.runs)}")
    print(f"  human minutes        {suite.human_minutes():.0f} at "
          f"{HUMAN_MINUTE_CENTS}c/min")
    print(f"  cost per success     {suite.cost_per_success():.2f} cents "
          f"(the right one)")
    print("\n  the escalated ticket reported succeeded, left the case open,")
    print("  refunded nothing, and consumed a specialist's attention. Cost")
    print("  per run cannot see any of that.")


def show_broken(
    propagated: SuiteResult,
    broken: SuiteResult,
    failures: list[str],
) -> None:
    """April, reproduced: the same money, and nobody to charge it to."""
    section("the same suite with context propagation broken")
    print(f"  {'':<22}{'propagated':>12}{'broken':>12}")
    rows = [
        ("total spend, cents", propagated.total_exact_cents(),
         broken.total_exact_cents()),
        ("unattributable", propagated.cost.unattributed_nanocents(
            propagated.roots) / 1e9,
         broken.cost.unattributed_nanocents(broken.roots) / 1e9),
        ("unattributable share", propagated.unattributed_share(),
         broken.unattributed_share()),
        ("trace completeness", propagated.completeness(),
         broken.completeness()),
    ]
    for label, left, right in rows:
        print(f"  {label:<22}{left:>12.4f}{right:>12.4f}")

    escalated = next(r for r in broken.runs if r.escalated)
    print(f"\n  the escalated run's tree now spans "
          f"{len(escalated.trace_ids)} traces, with no edge between them.")
    print("  To the backend it is two unrelated runs, and the expensive half")
    print("  is the half nobody can attribute to anything.")

    if propagated.total_exact_cents() != broken.total_exact_cents():
        failures.append(
            "breaking the edge changed the total spend; the whole point of "
            "the April story is that the money was all still there"
        )
    if propagated.unattributed_share() != 0.0:
        failures.append("the propagated suite left spend without an owner")
    if broken.unattributed_share() <= 0.0:
        failures.append(
            "the broken suite attributed everything, so it is no longer "
            "reproducing the failure"
        )


def show_completeness(
    propagated: SuiteResult,
    broken: SuiteResult,
    failures: list[str],
) -> None:
    """An SLI that measures your evidence rather than your service."""
    section("trace completeness as a service level indicator")
    print(f"  floor                {COMPLETENESS_FLOOR:.2f}")
    print(f"  propagated           {propagated.completeness():.2f}  "
          f"{'ok' if propagated.completeness() >= COMPLETENESS_FLOOR else 'PAGE'}")
    print(f"  broken               {broken.completeness():.2f}  "
          f"{'ok' if broken.completeness() >= COMPLETENESS_FLOOR else 'PAGE'}")
    for suite, label in ((propagated, "propagated"), (broken, "broken")):
        for run in suite.runs:
            if run.complete:
                continue
            print(f"    {label}: {run.run_id} incomplete "
                  f"traces={len(run.trace_ids)} "
                  f"missing={run.missing_attributes} "
                  f"no_receipt={run.writes_without_receipt}")

    if propagated.completeness() < COMPLETENESS_FLOOR:
        failures.append(
            f"trace completeness {propagated.completeness():.2f} is under "
            f"the {COMPLETENESS_FLOOR:.2f} floor with propagation intact"
        )
    if broken.completeness() >= COMPLETENESS_FLOOR:
        failures.append(
            "breaking propagation did not drop completeness below the floor"
        )
    for suite in (propagated, broken):
        for run in suite.runs:
            if run.writes_without_receipt:
                failures.append(
                    f"{run.run_id}: write span(s) with no side-effect id: "
                    f"{run.writes_without_receipt}"
                )


def main(argv: list[str] | None = None) -> int:
    args = argv if argv is not None else sys.argv[1:]
    print("Chapter 17 — which decision, by which version, spent this money")
    print(f"tickets: {[t.ticket_id for t in TICKETS]}")

    failures: list[str] = []
    exporter = chosen_exporter("memory")
    propagated = run_suite(propagate=True, exporter=exporter)
    broken = run_suite(propagate=False, exporter=exporter)

    show_wiring(exporter)
    show_tree(propagated, "--spans" in args)
    show_redaction(propagated, failures)
    show_cost(propagated)
    show_broken(propagated, broken, failures)
    show_completeness(propagated, broken, failures)

    print("\n--- what this proves ---")
    print("From the emitted spans alone you can say which agent version, on")
    print("whose behalf, under what remaining budget, spent how much and")
    print("moved which specific dollars — and you can see the one")
    print("instrumentation defect that makes that reconstruction")
    print("impossible while leaving every dashboard green.")

    if failures:
        print("\nFAILED:")
        for line in failures:
            print(f"  - {line}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
