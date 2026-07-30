"""``run_code``, and the contract that lives outside the code.

Give an agent a code-execution tool and a great deal gets easier at once.
Data can be filtered, joined, and aggregated before any of it touches the
context window. Loops that would have cost one turn each cost one turn total.
Glue nobody wants as a permanent tool gets written on the spot.

Now the honest part. Code execution does not add one risk to your system. It
converts every other risk into a larger one, because it dissolves the contract
``specs.py`` spends four hundred lines building. The input schema becomes "any
program", so validation, enums, and bounds stop constraining anything. The
``writes`` flag stops being knowable before the call, because whether the call
mutates the world depends on what the program does, which is decided after
policy has already allowed it. The result budget is whatever the program
prints. Idempotency is undefined. And the tool's real authority is not what its
description says: it is the union of everything reachable from the environment
the code runs in, including every credential in that environment, every host
the network permits, and the cloud metadata endpoint if nobody blocked it.

None of that argues against code execution. It argues that code execution is a
tool with a contract, and the contract lives outside the code.
:class:`SandboxContract` is those four terms as data: a declared environment,
deny-by-default egress, explicitly passed inputs, and budgeted output.

:class:`NullSandbox` is the weakest possible implementation of that contract
and it says so in its name. Chapter 12 replaces it with real isolation --
containers, syscall filtering, microVMs, managed sandboxes. What it does
provide is the one control that matters most and is also the cheapest:
there is no ``__import__`` in the execution namespace, so nothing in the
program can reach the network, the filesystem, or the process. What it cannot
provide is preemption: a program that loops forever runs forever, the wall
clock here is measured rather than enforced, and that gap is exactly the
argument for Chapter 12.
"""

from __future__ import annotations

import builtins
import io
import time
from collections.abc import Mapping
from contextlib import redirect_stdout
from dataclasses import dataclass, field
from typing import Any

from budget import count_tokens

__all__ = [
    "SAFE_BUILTIN_NAMES",
    "NullSandbox",
    "SandboxContract",
    "SandboxDenied",
    "run_code",
]

#: The only names a program gets. No ``__import__``, no ``open``, no ``exec``,
#: no ``eval``, no ``getattr``, no ``globals``. Deny by default, allowlist by
#: exception -- the same discipline as egress, applied to the namespace.
SAFE_BUILTIN_NAMES: tuple[str, ...] = (
    "abs",
    "all",
    "any",
    "bool",
    "dict",
    "divmod",
    "enumerate",
    "filter",
    "float",
    "int",
    "len",
    "list",
    "map",
    "max",
    "min",
    "print",
    "range",
    "repr",
    "reversed",
    "round",
    "set",
    "sorted",
    "str",
    "sum",
    "tuple",
    "zip",
)


class SandboxDenied(RuntimeError):
    """The sandbox refused to run the program, or the program broke a term.

    A distinct exception from a program error, because the two need different
    responses: a program error is something the model can fix on the next
    turn, and a denied term is something an operator has to decide about.
    """


@dataclass(frozen=True)
class SandboxContract:
    """The four terms of a code-execution contract, as data.

    Args:
        image: The base image, declared rather than ambient. "Whatever is on
            the box" is not an environment, it is a surprise.
        packages: Pinned dependencies. An unpinned dependency is a supply
            chain the model can reach.
        user: Never root.
        filesystem: ``"ephemeral"``. Nothing survives a call.
        egress: ``"deny"`` by default. The single highest-value control,
            because most of what makes code execution dangerous requires
            reaching the network.
        egress_allowlist: Hosts permitted by exception.
        environment: Variables the program sees. Empty, and the conformance
            check below is what keeps it that way: a sandbox with a database
            credential in its environment is not a code-execution tool, it is
            a database tool with an unbounded schema.
        max_stdout_tokens: Output cap.
        max_wall_seconds: Time cap. Measured here, enforced by Chapter 12.
    """

    image: str = "python:3.11-slim@sha256:northstar-sandbox-pinned"
    packages: tuple[str, ...] = ()
    user: str = "nobody"
    filesystem: str = "ephemeral"
    egress: str = "deny"
    egress_allowlist: tuple[str, ...] = ()
    environment: Mapping[str, str] = field(default_factory=dict)
    max_stdout_tokens: int = 300
    max_wall_seconds: float = 0.5

    def problems(self) -> list[str]:
        """Terms this contract gets wrong. Empty is the only safe answer.

        Checked rather than asserted in a design document, because the terms
        that get quietly relaxed are the ones nobody re-reads.
        """
        found: list[str] = []
        if self.egress not in ("deny", "allowlist"):
            found.append(
                f"egress is {self.egress!r}; it must be 'deny' or "
                "'allowlist'. Deny-by-default is the highest-value control."
            )
        if self.egress == "deny" and self.egress_allowlist:
            found.append("egress is denied but an allowlist is configured")
        if self.user == "root":
            found.append("sandbox user is root")
        if self.filesystem != "ephemeral":
            found.append(f"filesystem is {self.filesystem!r}, not ephemeral")
        if "@sha256:" not in self.image:
            found.append(f"image {self.image!r} is not pinned by digest")
        for name in self.environment:
            if _looks_like_a_credential(name):
                found.append(
                    f"environment carries {name!r}: a credential in the "
                    "sandbox converts a compute tool into a write tool with "
                    "an unbounded schema"
                )
        if self.max_stdout_tokens <= 0:
            found.append("max_stdout_tokens must be positive")
        if self.max_wall_seconds <= 0:
            found.append("max_wall_seconds must be positive")
        return found

    def to_dict(self) -> dict[str, Any]:
        """JSON-serialisable form, for an audit record or an approval."""
        return {
            "image": self.image,
            "packages": list(self.packages),
            "user": self.user,
            "filesystem": self.filesystem,
            "egress": self.egress,
            "egress_allowlist": list(self.egress_allowlist),
            "environment_keys": sorted(self.environment),
            "max_stdout_tokens": self.max_stdout_tokens,
            "max_wall_seconds": self.max_wall_seconds,
        }


_CREDENTIAL_HINTS: tuple[str, ...] = (
    "key",
    "token",
    "secret",
    "password",
    "credential",
    "dsn",
    "url",
    "conn",
)


def _looks_like_a_credential(name: str) -> bool:
    """Whether an environment variable name smells like a credential."""
    lowered = name.lower()
    return any(hint in lowered for hint in _CREDENTIAL_HINTS)


class NullSandbox:
    """No isolation worth the name. Chapter 12 replaces it.

    Named for what it is. It exists so the tool contract in ``specs.py`` has
    something behind it, and so the checks that matter -- no imports, no
    credentials, budgeted output, explicit inputs -- are demonstrable offline
    without a container runtime.

    Args:
        contract: The declared terms. Refused at construction if any term is
            wrong, because a sandbox that starts with a bad contract is worse
            than one that does not start.

    Raises:
        SandboxDenied: If :meth:`SandboxContract.problems` is non-empty.
    """

    def __init__(self, contract: SandboxContract | None = None) -> None:
        self.contract = contract or SandboxContract()
        problems = self.contract.problems()
        if problems:
            raise SandboxDenied(
                "sandbox contract is not safe to run: "
                + "; ".join(problems)
            )
        self.runs = 0

    def run(
        self,
        program: str,
        inputs: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Execute ``program`` with ``inputs`` bound, and nothing else.

        Args:
            program: The source. Passed in by the model.
            inputs: Data the *caller* chose. The program cannot fetch what it
                likes, which is the third term of the contract.

        Returns:
            ``{"stdout", "wall_seconds", "truncated"}``. Output is capped and
            shaped like any other tool result.

        Raises:
            SandboxDenied: If the program raises. The message carries the
                exception type and text and nothing else -- no traceback, no
                internal paths -- because every byte of an error goes into the
                model's context and from there into summaries and traces.
        """
        self.runs += 1
        namespace: dict[str, Any] = {
            "__builtins__": {
                name: getattr(builtins, name) for name in SAFE_BUILTIN_NAMES
            },
            "inputs": dict(inputs or {}),
        }
        captured = io.StringIO()
        started = time.monotonic()
        try:
            with redirect_stdout(captured):
                exec(compile(program, "<sandbox>", "exec"), namespace)
        except Exception as exc:  # noqa: BLE001 - the boundary catches all
            raise SandboxDenied(
                f"{type(exc).__name__}: {exc}"
            ) from None
        elapsed = time.monotonic() - started

        stdout = captured.getvalue()
        cap = self.contract.max_stdout_tokens
        truncated = count_tokens(stdout) > cap
        if truncated:
            stdout = stdout[: cap * 4] + (
                f"\n[stdout truncated to {cap} tokens]"
            )
        return {
            "stdout": stdout,
            "wall_seconds": round(elapsed, 4),
            "truncated": truncated,
        }


def run_code(
    program: str,
    inputs: dict[str, Any] | None = None,
    *,
    sandbox: NullSandbox,
) -> dict[str, Any]:
    """The tool implementation. Thin, because the contract is elsewhere."""
    return sandbox.run(program, inputs)
