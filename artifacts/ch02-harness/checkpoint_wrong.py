"""The harness that produced the chapter's opening incident.

    14:02:11  worker-7  SIGTERM, draining
    14:07:03  worker-3  resumed run_01HZ7Q3M step=6

Seven of those sixty runs came back and repeated a step they had already
taken. The cause was one ordering choice nobody made on purpose: the harness
appended the model's response, saved, and *then* dispatched the tool calls
that response contained. A run killed between the save and the dispatch
resumed with the decision on the record and no record of whether the
decision had been carried out. The resumed loop read the last assistant
message, found an unanswered call, and made it.

This module is that harness, so the demo can run it against the same fault
and diff the ledger. Two defects, and they compound:

1. **The checkpoint sits between the decision and the outcome.** So the
   resumed run inherits an intent and no evidence, and no way to tell "it
   landed" from "it may have".
2. **The call carries no idempotency key.** So re-dispatching is a second
   intent rather than a lookup of the first one's receipt.

Fixing either one alone is not enough, and that is the point of shipping
both. Move the checkpoint and the resumed run still cannot settle an
ambiguous write. Add the key and the resumed run is safe, but it is safe by
accident, because nothing recorded that the write had been attempted.
"""

from __future__ import annotations

from checkpoint import SqliteCheckpointer
from loop import HarnessLoop
from northstar_contracts import Message, RunState, ToolCall
from registry import HarnessRegistry

__all__ = ["WrongOrderLoop", "resume_from_history", "unkeyed"]


def unkeyed(tools: HarnessRegistry) -> HarnessRegistry:
    """A registry that never stamps an idempotency key.

    Chapter 1's registry, and the one Northstar shipped. The refund tool
    accepts a key; nothing supplies one; a retry after a timeout is a second
    intent as far as the refund service can tell, and the refund service is
    the only party in a position to tell.
    """

    class _Unkeyed(type(tools)):    # type: ignore[misc]
        def stamp(self, call: ToolCall, run_id: str, step_id: str) -> ToolCall:
            """Return the call untouched. The whole defect, as one method."""
            return call

    plain = _Unkeyed(tools.policy, tools.principal)
    return plain.register_all(tools.bindings())


class WrongOrderLoop(HarnessLoop):
    """Checkpoints after the model call and before the dispatch.

    Everything else — the same tools, the same budget, the same SQLite file,
    the same scripted model — is identical to :class:`~loop.HarnessLoop`.
    Only the position of the writes differs, and that is enough to turn a
    resumable run into a repeated side effect.
    """

    def step(self) -> RunState:
        """One turn, with the checkpoint where it loses the outcome."""
        self.budget.check(self.state)
        self.state.step += 1
        response = self.model.complete(
            self.state.messages, self.tools.specs()
        )
        self.ledger.record(
            self.model_name, response.input_tokens, response.output_tokens
        )
        self.state.messages.append(
            Message(role="assistant", content=response.content)
        )

        # Here is the defect. The model's decision is now durable and
        # nothing has been dispatched, so a worker that dies on the next
        # line comes back believing it has a decision to carry out.
        self.checkpointer.save(self.state)

        if not response.tool_calls:
            self.state.status = "succeeded"
            self.checkpointer.save(self.state)
            return self.state

        for call in response.tool_calls:
            # No intent record, and none needed by this design: the pending
            # call is in the message history. What the history cannot say is
            # whether the call ran.
            result = self.tools.dispatch(call)
            self.state.messages.append(
                Message(role="tool", content=result)
            )
        self.checkpointer.save(self.state)
        return self.state


def resume_from_history(
    loop: WrongOrderLoop,
    checkpointer: SqliteCheckpointer,
    run_id: str,
) -> RunState:
    """Resume the way a harness with no journal has to: from the transcript.

    Read the last assistant message, find the tool calls it asked for that
    have no answer, and make them. This is not a strawman; it is the obvious
    implementation, and it is what shipped.

    Returns:
        The state the resumed run reached. The refund service will have paid
        twice, and the run will report ``succeeded``.
    """
    state = checkpointer.load(run_id)
    if state is None:
        raise LookupError(f"no checkpoint for run {run_id!r}")

    answered = {
        message.content.call_id
        for message in state.messages
        if message.role == "tool" and hasattr(message.content, "call_id")
    }
    for message in reversed(state.messages):
        pending = [c for c in message.tool_calls if c.id not in answered]
        if not pending:
            continue
        for call in pending:
            result = loop.tools.dispatch(call)
            state.messages.append(Message(role="tool", content=result))
        break
    checkpointer.save(state)
    return loop.run_from(state)
