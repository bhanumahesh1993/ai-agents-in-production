"""The approval fingerprint: a canonical hash over what will change.

The June incident had one root cause. The decision was bound to a run and
the action was not, so an approval granted for a 24,000-cent refund was
still an approval when the resumed run decided on 31,200. The repair is a
hash over exactly what will happen in the world, computed before the
request is raised and recomputed before the call executes.

Canonicalization is where this gets subtle, because a hash is only as
stable as the serialization under it. This module does not implement that
serialization: :func:`northstar_contracts.canonical_json` already sorts
keys, uses tight separators, escapes non-ASCII, and refuses anything it
cannot encode unambiguously, and
:func:`northstar_policy.approval_fingerprint` already hashes a call's
canonical JSON with sha256. Reimplementing either here would be two
canonicalizers in one repository, which is one more than any system can
afford.

What this module adds is the *body*: which facts go under the hash.

    hash everything that changes what happens in the world,
    and nothing that does not.

In: the canonicalization version, the tool name, the tool version, the
arguments, the principal the call runs as, and the run it belongs to.
Out: trace identifiers, the model's message id, the call's per-attempt id,
and timestamps. ``run_id`` is in and ``step`` is out, deliberately —
binding to the run stops an approval for one customer being replayed in
another, and leaving the step out means a durable replay of the same step
reuses the decision instead of waking a specialist at 3 a.m. Single use is
enforced separately, by :class:`inbox.TaskInbox`, against the pair of
fingerprint and step.
"""

from __future__ import annotations

from typing import Any

from northstar_contracts import ToolCall, ToolSpec
from northstar_policy import Principal, approval_fingerprint

__all__ = [
    "CANON_VERSION",
    "ToolVersions",
    "bind",
    "fingerprint",
]

#: Version of the canonicalization itself, inside the hashed body. Changing
#: how the body is built is a deliberate migration that invalidates every
#: pending approval, rather than a silent one that invalidates them anyway.
CANON_VERSION = 1


def bind(
    call: ToolCall,
    principal: Principal,
    tool_version: str,
) -> ToolCall:
    """Wrap a call with the envelope that belongs under the hash.

    The result is a :class:`ToolCall` because that is what
    :func:`northstar_policy.approval_fingerprint` hashes, and reusing it is
    the whole point: one canonicalizer, one hash function, one definition
    of what a fingerprint is.

    The call's ``id`` survives into the bound call and is then ignored,
    because ``approval_fingerprint`` excludes it. A replay mints a new call
    id for the same intent, and an approval a replay invalidates is useless
    in a durable system.
    """
    return ToolCall(
        id=call.id,
        name=call.name,
        arguments={
            "canon": CANON_VERSION,
            "tool_version": tool_version,
            "principal": principal.user_id,
            "arguments": dict(call.arguments),
        },
    )


def fingerprint(
    call: ToolCall,
    principal: Principal,
    run_id: str,
    tool_version: str,
) -> str:
    """Canonical hash of exactly what will happen in the world.

    Args:
        call: The call as it will be dispatched, arguments included.
        principal: Who the call runs as. A refund approved for one
            customer's run is not a refund approved for another's.
        run_id: The run the call belongs to.
        tool_version: The declared version of the tool contract. A version
            bump is a semantics change, so it invalidates parked approvals
            on purpose.

    Returns:
        A 64-character sha256 hex digest.
    """
    return approval_fingerprint(bind(call, principal, tool_version), run_id)


class ToolVersions:
    """The declared version of each tool, and a way to bump one.

    A tool version lives in :class:`~northstar_contracts.models.ToolSpec`,
    which is where the model and the runtime both read it. This class is a
    thin index over those specs plus an override map, so the demo can bump
    ``issue_refund`` from 3 to 4 and watch a parked approval stop binding
    without anyone editing a frozen dataclass.

    Args:
        specs: The tool contracts in play.
        overrides: Name to version, applied over the specs. Northstar's
            refund tool is on version 3 in this chapter.
    """

    def __init__(
        self,
        specs: list[ToolSpec] | None = None,
        overrides: dict[str, str] | None = None,
    ) -> None:
        self._declared = {s.name: s.version for s in (specs or [])}
        self._overrides: dict[str, str] = dict(overrides or {})

    def version(self, tool: str) -> str:
        """The version of one tool.

        Raises:
            KeyError: On an unknown tool. The boundary fails closed on an
                unknown tool or an unknown tool version, and returning a
                default here would be exactly that failure, silently.
        """
        if tool in self._overrides:
            return self._overrides[tool]
        if tool in self._declared:
            return self._declared[tool]
        raise KeyError(f"no declared version for tool {tool!r}")

    def bump(self, tool: str) -> str:
        """Move a tool to its next version. Returns the new version."""
        current = self.version(tool)
        nxt = str(int(current) + 1) if current.isdigit() else f"{current}+1"
        self._overrides[tool] = nxt
        return nxt

    def to_dict(self) -> dict[str, Any]:
        """Every version in play, for the payload's envelope."""
        return {**self._declared, **self._overrides}
