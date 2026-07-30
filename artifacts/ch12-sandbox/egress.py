"""Deny-by-default egress, shared by every rung of the ladder.

The policy is a separate object from the sandbox so that the network rule
does not change when the isolation rung does. It reuses ``Decision`` from
``northstar_policy``, because an egress decision is a policy decision and
there is no reason to invent a second vocabulary for it.

Two details carry the security property and both are easy to get wrong:

* every resolved address is checked, not the first one. A name with one
  permitted and one private answer is a DNS rebinding attack that a
  first-answer check waves through.
* a name that does not resolve is denied. "Could not check" is not
  "safe to allow".

Resolution goes through an injectable resolver so the suite runs offline
with no DNS at all. In production the same object sits inside a forward
proxy that resolves the name itself on every request, including after a
redirect.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from ipaddress import ip_address, ip_network

from northstar_policy import Decision

__all__ = [
    "BLOCKED",
    "EgressPolicy",
    "Resolver",
    "StaticResolver",
    "is_blocked",
    "no_egress",
]

BLOCKED = [
    ip_network(n)
    for n in (
        "127.0.0.0/8",
        "10.0.0.0/8",
        "172.16.0.0/12",
        "192.168.0.0/16",
        "169.254.0.0/16",
        "::1/128",
        "fc00::/7",
    )
]

#: Anything that turns a hostname into a list of address strings.
Resolver = Callable[[str], list[str]]


class StaticResolver:
    """DNS replaced by a table, because this repository runs offline.

    A literal address resolves to itself, which is what stops
    ``https://169.254.169.254/`` from sailing past a name-based check.
    Anything else is looked up in the table, and an unknown name resolves
    to nothing, which :meth:`EgressPolicy.decide` treats as a deny.
    """

    def __init__(self, table: Mapping[str, Sequence[str]] | None = None) -> None:
        self._table: dict[str, list[str]] = {
            host: list(addrs) for host, addrs in (table or {}).items()
        }

    def add(self, host: str, *addrs: str) -> StaticResolver:
        """Record ``host`` as resolving to ``addrs``. Returns ``self``."""
        self._table[host] = list(addrs)
        return self

    def table(self) -> dict[str, list[str]]:
        """A copy of the table, for serialising into a child process."""
        return {host: list(addrs) for host, addrs in self._table.items()}

    def __call__(self, host: str) -> list[str]:
        """Every A and AAAA answer for ``host``; empty when unknown."""
        try:
            return [str(ip_address(host))]
        except ValueError:
            return list(self._table.get(host, ()))


def is_blocked(addr: str) -> bool:
    """Whether ``addr`` falls in a range a sandbox has no business reaching."""
    try:
        parsed = ip_address(addr)
    except ValueError:
        return True  # unparseable is not permitted; fail closed
    return any(parsed in net for net in BLOCKED)


class EgressPolicy:
    """Allow a short enumerated list of hosts on 443, and nothing else."""

    def __init__(
        self,
        allow_hosts: frozenset[str],
        *,
        resolver: Resolver | None = None,
    ) -> None:
        """Build a policy.

        Args:
            allow_hosts: The enumerated allowlist. The empty set means no
                egress, and is the default construction for a sandbox
                summarising a customer's own spreadsheet.
            resolver: How names become addresses. Injectable so the tests
                never touch real DNS; defaults to an empty static table,
                which denies every name.
        """
        self.allow_hosts = allow_hosts  # empty set means no egress
        self.resolver: Resolver = resolver or StaticResolver()

    def decide(self, host: str, port: int) -> Decision:
        """Allow or deny one outbound request to ``host`` on ``port``."""
        if port != 443:
            return Decision.DENY
        addrs = self.resolver(host)
        if not addrs:
            return Decision.DENY  # unresolvable: cannot check, so refuse
        for addr in addrs:  # every A and AAAA answer
            if is_blocked(addr):
                return Decision.DENY  # SSRF and metadata
        if host not in self.allow_hosts:
            return Decision.DENY  # default is deny
        return Decision.ALLOW


def no_egress(resolver: Resolver | None = None) -> EgressPolicy:
    """The default construction: ``EgressPolicy(allow_hosts=frozenset())``."""
    return EgressPolicy(frozenset(), resolver=resolver)
