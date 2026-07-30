"""Where the egress hook goes, and how it reports a deny.

In production the hook is a forward proxy that the sandbox is the only
route to: the network layer denies everything, the proxy sees the host on
every request, resolves it itself, and logs the decision. None of that
can be stood up offline in a book's test suite, so this module puts the
same decision in the same place in the request path -- in front of
``urllib.request.urlopen``, inside the environment that runs the code --
and routes anything the policy allows to a loopback stand-in.

What that buys is a real test: the metadata service the suite denies is
running, on loopback, reachable, and the deny comes from the policy. What
it does not buy is containment against code that opens a raw socket, and
the README says so rather than pretending otherwise.

A deny is reported to the parent process as a marker line on stderr,
because a subprocess boundary has exactly two channels and the deny list
has to survive the trip.
"""

from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from egress import EgressPolicy, StaticResolver
from northstar_policy import Decision

__all__ = [
    "CHILD_PRELUDE",
    "DENY_PREFIX",
    "EgressDenied",
    "NetworkConfig",
    "install",
    "report_deny",
    "split_denies",
]

DENY_PREFIX = "__northstar_egress_deny__ "

DEFAULT_PORTS = {"https": 443, "http": 80}


class EgressDenied(RuntimeError):
    """Raised inside the sandbox when the policy refuses a request."""

    def __init__(self, host: str, port: int) -> None:
        super().__init__(f"egress denied: {host}:{port}")
        self.host = host
        self.port = port


@dataclass(frozen=True)
class NetworkConfig:
    """The network the sandboxed code sees.

    Args:
        policy: The egress policy to enforce, or ``None`` for the
            in-process negative control, which enforces nothing.
        routes: Where an allowed host actually lives while the suite is
            offline: host to ``(address, port)`` on loopback.
    """

    policy: EgressPolicy | None
    routes: Mapping[str, tuple[str, int]] = field(default_factory=dict)

    def to_json(self) -> str:
        """Serialise for a child process. The credential-free half."""
        table: dict[str, list[str]] = {}
        allow: list[str] = []
        if self.policy is not None:
            resolver = self.policy.resolver
            if not isinstance(resolver, StaticResolver):
                raise TypeError(
                    "only a StaticResolver can cross a process boundary; "
                    "a live resolver belongs in the proxy, not in the child"
                )
            table = resolver.table()
            allow = sorted(self.policy.allow_hosts)
        payload: dict[str, Any] = {
            "enforce": self.policy is not None,
            "allow_hosts": allow,
            "resolve": table,
            "routes": {h: list(v) for h, v in self.routes.items()},
        }
        return json.dumps(payload, sort_keys=True)

    @staticmethod
    def from_json(blob: str) -> NetworkConfig:
        """Rebuild inside the child. The inverse of :meth:`to_json`."""
        payload = json.loads(blob)
        policy: EgressPolicy | None = None
        if payload["enforce"]:
            policy = EgressPolicy(
                frozenset(payload["allow_hosts"]),
                resolver=StaticResolver(payload["resolve"]),
            )
        routes = {h: (v[0], int(v[1])) for h, v in payload["routes"].items()}
        return NetworkConfig(policy=policy, routes=routes)


def target_of(url: str) -> tuple[str, int]:
    """The ``(host, port)`` a URL asks for, with the scheme default."""
    parts = urlsplit(url)
    host = parts.hostname or ""
    port = parts.port or DEFAULT_PORTS.get(parts.scheme, 0)
    return host, port


def _rewrite(url: str, route: tuple[str, int]) -> str:
    """Point an allowed URL at its loopback stand-in.

    The policy has already evaluated the request the code actually made,
    including its scheme and port. This only decides which socket carries
    it, and offline that is always a loopback stub speaking plain HTTP.
    """
    parts = urlsplit(url)
    address, port = route
    return urlunsplit(("http", f"{address}:{port}", parts.path, parts.query, ""))


def report_deny(host: str) -> None:
    """Report a deny across a process boundary, on stderr."""
    sys.stderr.write(DENY_PREFIX + host + "\n")
    sys.stderr.flush()


def split_denies(stderr: str) -> tuple[str, list[str]]:
    """Split marker lines out of captured stderr. Returns (stderr, hosts)."""
    kept: list[str] = []
    denies: list[str] = []
    for line in stderr.splitlines():
        if line.startswith(DENY_PREFIX):
            denies.append(line[len(DENY_PREFIX) :].strip())
        else:
            kept.append(line)
    text = "\n".join(kept)
    if stderr.endswith("\n") and text:
        text += "\n"
    return text, denies


def install(
    net: NetworkConfig,
    on_deny: Callable[[str], None],
) -> Callable[[], None]:
    """Route ``urlopen`` through the policy. Returns an undo callable."""
    real = urllib.request.urlopen

    def guarded(
        url: Any,
        data: Any = None,
        timeout: float = 5.0,
        **kwargs: Any,
    ) -> Any:
        """Stand-in for ``urlopen`` that asks the policy first."""
        as_str = url if isinstance(url, str) else getattr(url, "full_url", "")
        host, port = target_of(as_str)
        if net.policy is not None:
            if net.policy.decide(host, port) is Decision.DENY:
                on_deny(host)
                raise EgressDenied(host, port)
        route = net.routes.get(host)
        if route is None:
            raise urllib.error.URLError(
                f"no offline route to {host!r}; this suite has no network"
            )
        return real(_rewrite(as_str, route), data, timeout=timeout, **kwargs)

    urllib.request.urlopen = guarded  # type: ignore[assignment]

    def undo() -> None:
        """Put the real ``urlopen`` back."""
        urllib.request.urlopen = real  # type: ignore[assignment]

    return undo


# The first thing a child interpreter runs. It installs the egress hook
# and the resource limits *before* reading the code it was asked to run,
# then drops the library directories back off sys.path so the sandboxed
# code has to work a little harder to find the shim it is inside.
CHILD_PRELUDE = """\
import os
import resource
import sys

_lib = os.environ.pop("NORTHSTAR_SANDBOX_LIB", "")
_added = [p for p in _lib.split(os.pathsep) if p]
for _p in reversed(_added):
    sys.path.insert(0, _p)
import netshim
for _p in _added:
    if _p in sys.path:
        sys.path.remove(_p)

netshim.install(
    netshim.NetworkConfig.from_json(os.environ.pop("NORTHSTAR_SANDBOX_NET")),
    netshim.report_deny,
)

_fsize = int(os.environ.pop("NORTHSTAR_SANDBOX_FSIZE", "0"))
if _fsize:
    resource.setrlimit(resource.RLIMIT_FSIZE, (_fsize, _fsize))
_cpu = int(os.environ.pop("NORTHSTAR_SANDBOX_CPU", "0"))
if _cpu:
    resource.setrlimit(resource.RLIMIT_CPU, (_cpu, _cpu))

exec(compile(sys.stdin.read(), "<sandboxed>", "exec"), {"__name__": "__main__"})
"""
