"""The secrets broker, and the environment scrub that goes with it.

The rule is short: the sandbox gets a scoped token, never the credential.

A process outside the sandbox holds the long-lived credential. When code
inside needs to call an internal service, it asks the broker, which checks
the principal, mints a short-lived token bound to one audience and one
scope, and hands back only that. If the token leaks, the blast radius is
one API, for one minute, at one permission level.

The second half of the same rule is that environment variables are not a
secrets mechanism for sandboxes: every process inside the environment can
read the whole environment, including the process that got there through
row 41. :func:`scrub_env` is what the subprocess rung uses to build the
child's environment, and the test asserts on its effect rather than on its
existence.
"""

from __future__ import annotations

import hashlib
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field

from northstar_policy import Principal

__all__ = [
    "SAFE_ENV_NAMES",
    "SECRET_NAME_PATTERNS",
    "ScopeNotHeld",
    "ScopedToken",
    "SecretsBroker",
    "looks_like_secret",
    "scrub_env",
]

# The repository's secret-name patterns. Substring matched, upper-cased,
# because a deny list of exact names is a list somebody will forget to
# add to on the day it matters.
SECRET_NAME_PATTERNS = (
    "API_KEY",
    "APIKEY",
    "ACCESS_KEY",
    "AUTH",
    "CREDENTIAL",
    "PASSWD",
    "PASSWORD",
    "PRIVATE_KEY",
    "SECRET",
    "SESSION",
    "TOKEN",
)

# The only names that survive the scrub. An allowlist, for the same
# reason the egress policy is an allowlist.
SAFE_ENV_NAMES = ("PATH", "LANG", "LC_ALL", "TZ")


def looks_like_secret(name: str) -> bool:
    """Whether an environment variable name looks like it holds a secret."""
    upper = name.upper()
    return any(pattern in upper for pattern in SECRET_NAME_PATTERNS)


def scrub_env(
    env: Mapping[str, str],
    *,
    extra: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Build a child environment from an allowlist, then add ``extra``.

    Nothing from the parent environment survives except the names in
    :data:`SAFE_ENV_NAMES`, so a new API key added to the deployment
    template next quarter does not quietly become reachable from inside
    the sandbox.
    """
    clean = {
        name: value
        for name, value in env.items()
        if name in SAFE_ENV_NAMES and not looks_like_secret(name)
    }
    for name, value in (extra or {}).items():
        if looks_like_secret(name):
            raise ValueError(f"refusing to pass {name!r} into a sandbox")
        clean[name] = value
    return clean


class ScopeNotHeld(PermissionError):
    """The principal asked the broker for a scope it was not granted."""


@dataclass(frozen=True)
class ScopedToken:
    """A short-lived, audience-bound, single-scope bearer token."""

    value: str
    audience: str
    scope: str
    expires_at: float
    issued_to: str

    def is_valid_for(
        self,
        audience: str,
        scope: str,
        *,
        now: float | None = None,
    ) -> bool:
        """Whether this token authorises ``scope`` at ``audience``, now."""
        clock_now = time.monotonic() if now is None else now
        return (
            self.audience == audience
            and self.scope == scope
            and clock_now < self.expires_at
        )


@dataclass
class SecretsBroker:
    """Holds the long-lived credential so the sandbox never has to.

    Args:
        credential: The long-lived secret. It lives here, outside the
            boundary, and no method on this class returns it.
        ttl_s: Token lifetime in seconds. Seconds to minutes, not hours.
        clock: Monotonic clock, injectable so the expiry test does not
            sleep.
    """

    credential: str
    ttl_s: float = 60.0
    clock: Callable[[], float] = time.monotonic
    audit: list[dict[str, object]] = field(default_factory=list)

    def mint(
        self,
        principal: Principal,
        *,
        audience: str,
        scope: str,
    ) -> ScopedToken:
        """Mint one token, or refuse.

        Raises:
            ScopeNotHeld: If the principal does not hold ``scope``. The
                code-execution principal holds ``sandbox.exec`` and
                nothing else, so this is what stops a broker call from
                becoming an escalation path.
        """
        if not principal.has(scope):
            self.audit.append(
                {
                    "event": "mint.denied",
                    "agent_id": principal.agent_id,
                    "audience": audience,
                    "scope": scope,
                }
            )
            raise ScopeNotHeld(
                f"{principal.agent_id} does not hold scope {scope!r}"
            )
        now = self.clock()
        material = f"{self.credential}|{audience}|{scope}|{len(self.audit)}"
        token = ScopedToken(
            value=hashlib.sha256(material.encode()).hexdigest()[:32],
            audience=audience,
            scope=scope,
            expires_at=now + self.ttl_s,
            issued_to=principal.agent_id,
        )
        self.audit.append(
            {
                "event": "mint.ok",
                "agent_id": principal.agent_id,
                "audience": audience,
                "scope": scope,
                "expires_in_s": self.ttl_s,
            }
        )
        return token
