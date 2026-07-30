"""The subagent factory: isolation enforced by a registry, not by a prompt.

Three details carry the design.

The write check is at *registration* time, so a research worker fails when it
is assembled rather than when it refunds somebody. This is the isolation
boundary expressed as code.

The per-worker ``budget_cents`` bounds the fan-out, so three workers cannot
quietly become nine.

And :func:`compress` returns a :class:`Finding` — a short claim and
references to evidence — never the worker's message list. That is the
direction rule from the chapter's table: outbound carries a goal, a
constraint set, a budget, and an identity; inbound carries a compressed
finding and evidence references, not the raw trace. The orchestrator's
context is the scarce resource in the whole system, and a subagent that
returns everything it saw has cost two model calls and bought nothing.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from northstar_contracts import (
    Money,
    RunState,
    ToolCall,
    ToolSpec,
    World,
    estimate_tokens,
)
from northstar_runtime import AgentLoop, FakeModel, ToolRegistry

__all__ = [
    "FINDING_TOKEN_BUDGET",
    "TRUNCATION_NOTE",
    "NORTHSTAR_READS",
    "READER_SCRIPTS",
    "Finding",
    "WriteToolInReader",
    "compress",
    "read_bindings",
    "reader_registry",
    "spawn_reader",
]

#: The three tools a research worker may hold. Every one of them reads.
NORTHSTAR_READS = ("get_order", "get_policy", "search_orders")

#: What a finding is allowed to cost the orchestrator. Enforced in the
#: dispatch path, not requested in a prompt.
FINDING_TOKEN_BUDGET = 400

#: Appended when a claim had to be cut. The orchestrator should be able to
#: tell a short answer from a shortened one.
TRUNCATION_NOTE = " [truncated to fit the finding budget]"


class WriteToolInReader(RuntimeError):
    """A write tool was offered to a read-only worker.

    The chapter prints this check as ``assert not spec.writes``. The shipped
    version raises, because ``python -O`` erases assertions and this is a
    permission boundary rather than a debugging aid.
    """

    def __init__(self, tool: str) -> None:
        super().__init__(
            f"{tool} mutates the world and cannot be registered on a "
            f"read-only research worker"
        )


@dataclass(frozen=True)
class Finding:
    """What crosses the boundary on the way back.

    Args:
        question: The scoped question the worker was given.
        claim: One sentence. What the worker concluded.
        evidence_refs: Pointers to what it read, not copies of it.
        worker_tokens: Everything the worker's own context held.
        budget_cents: What the worker was allowed to spend.
        ok: ``False`` when the worker failed. The orchestrator proceeds with
            the evidence it has and says so, because a read that never
            happened costs nothing to discard.
    """

    question: str
    claim: str
    evidence_refs: tuple[str, ...] = ()
    worker_tokens: int = 0
    budget_cents: Money = 0
    ok: bool = True
    tool_calls: tuple[str, ...] = field(default=())

    @property
    def summary(self) -> str:
        """The text the orchestrator actually reads."""
        refs = ", ".join(self.evidence_refs) or "(none)"
        return f"Q: {self.question}\nA: {self.claim}\nEvidence: {refs}"

    @property
    def tokens(self) -> int:
        """What this finding costs the orchestrator's window."""
        return estimate_tokens(self.summary)

    @property
    def compression(self) -> float:
        """Worker context divided by what crossed the boundary."""
        return self.worker_tokens / max(1, self.tokens)


def read_bindings(world: World) -> list[tuple[ToolSpec, Callable[..., Any]]]:
    """The read tools, taken straight from the world's own bindings."""
    return [
        (spec, fn) for spec, fn in world.tools() if spec.name in NORTHSTAR_READS
    ]


def reader_registry(
    bindings: list[tuple[ToolSpec, Callable[..., Any]]],
) -> ToolRegistry:
    """Build a registry that physically cannot dispatch a write.

    Raises:
        WriteToolInReader: On the first write tool offered. At assembly
            time, which is the whole point: the alternative is finding out
            at refund time.
    """
    reads = ToolRegistry()
    for spec, fn in bindings:          # get_order, get_policy,
        if spec.writes:                # search_orders
            raise WriteToolInReader(spec.name)
        reads.register(spec, fn)
    return reads


def _call(name: str, arguments: dict[str, Any], index: int) -> ToolCall:
    """One scripted worker call."""
    return ToolCall(f"r{index}", name, arguments)


#: One script per research question, matched by substring. Each worker does
#: several paginated reads, which is the point: the paginated output is what
#: must *not* reach the orchestrator.
READER_SCRIPTS: dict[str, list[Any]] = {
    "more than one refund event": [
        _call("search_orders", {"customer_id": "CUST-8841", "page": 1}, 1),
        _call("search_orders", {"customer_id": "CUST-8841", "page": 2}, 2),
        _call("get_order", {"order_id": "NR-2026-0041827"}, 3),
        _call("get_order", {"order_id": "NR-2026-0041903"}, 4),
        "No June order carries two refund events today; NR-2026-0041827 is "
        "the one with the history that produced the July incident.",
    ],
    "duplicate refund, by sku": [
        _call("get_policy", {"reason": "damaged"}, 1),
        _call("get_policy", {"reason": "changed_mind", "sku": "NR-MUG-02"}, 2),
        _call("get_policy", {}, 3),
        "Policy allows one refund per claim at 100 percent for damaged "
        "goods; nothing in it authorises a second row for the same claim.",
    ],
    "still have an open ticket": [
        _call("search_orders", {"flag": "fraud_review", "page": 1}, 1),
        _call("get_order", {"order_id": "NR-2026-0042110"}, 2),
        "Only NR-2026-0042110 is still open, and it is held for fraud "
        "review rather than for a refund decision.",
    ],
}


def compress(state: RunState, max_tokens: int = FINDING_TOKEN_BUDGET) -> Finding:
    """Turn a worker's whole run into the one thing worth carrying back.

    The claim is the worker's final answer. The evidence references are
    derived from what it actually called, so they point at the reads rather
    than paraphrasing them. Everything else — every page of
    ``search_orders``, every order body — stays on the worker's side of the
    boundary and is discarded with it.
    """
    calls: list[str] = []
    refs: list[str] = []
    for message in state.messages:
        for call in message.tool_calls:
            calls.append(call.name)
            order_id = call.arguments.get("order_id")
            if order_id:
                refs.append(f"artifact://orders/{order_id}")
            elif call.name == "get_policy":
                refs.append("artifact://policy/2026-07-01")
            elif call.name == "search_orders":
                refs.append("artifact://search/customer-orders")

    claim = (state.final_text or "").strip() or "worker produced no finding"
    question = _question_of(state)
    finding = Finding(
        question=question,
        claim=claim,
        evidence_refs=tuple(dict.fromkeys(refs)),
        worker_tokens=estimate_tokens([m.content for m in state.messages]),
        ok=state.status == "succeeded",
        tool_calls=tuple(calls),
    )
    if finding.tokens <= max_tokens:
        return finding
    # Over budget. Cut the claim rather than the evidence pointers: a
    # reference is what lets the orchestrator go and look, and a truncated
    # reference points at nothing. Shrink until it fits rather than
    # computing a length, so the note's own cost is inside the budget too.
    kept = claim
    while kept:
        candidate = Finding(
            question=question,
            claim=kept + TRUNCATION_NOTE,
            evidence_refs=finding.evidence_refs,
            worker_tokens=finding.worker_tokens,
            ok=finding.ok,
            tool_calls=finding.tool_calls,
        )
        if candidate.tokens <= max_tokens:
            return candidate
        kept = kept[: len(kept) // 2]
    return Finding(
        question=question,
        claim=TRUNCATION_NOTE.strip(),
        evidence_refs=finding.evidence_refs,
        worker_tokens=finding.worker_tokens,
        ok=finding.ok,
        tool_calls=finding.tool_calls,
    )


def _question_of(state: RunState) -> str:
    """The scoped question the worker was given."""
    for message in state.messages:
        if message.role == "user" and isinstance(message.content, str):
            return message.content
    return ""


def spawn_reader(
    world: World,
    question: str,
    budget_cents: Money = 20,
) -> Finding:
    """Run a worker in its own context; return a summary only."""
    reads = reader_registry(read_bindings(world))
    worker = AgentLoop(
        model=FakeModel(READER_SCRIPTS),
        tools=reads,
        max_turns=6,
        budget_cents=budget_cents,
    )
    state = worker.run(question)
    finding = compress(state, max_tokens=FINDING_TOKEN_BUDGET)
    return Finding(
        question=finding.question,
        claim=finding.claim,
        evidence_refs=finding.evidence_refs,
        worker_tokens=finding.worker_tokens,
        budget_cents=budget_cents,
        ok=finding.ok,
        tool_calls=finding.tool_calls,
    )
