"""A fake authorization server: RFC 8693 token exchange, offline.

The real thing is a network service with signing keys. This is the same
*contract* with the cryptography replaced by a dictionary, because the
properties the chapter is about are not cryptographic:

* a token names one audience, and every other audience rejects it;
* a token carries one scope, and it is narrower than the subject's grant;
* a token expires in seconds, not hours;
* a token records both parties, ``sub`` and ``act``.

Each of those is enforced below, and each has a test.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from northstar_contracts import short_hash

__all__ = [
    "AudienceMismatch",
    "AuthorizationServer",
    "ExpiredToken",
    "InsufficientScope",
    "ScopedToken",
    "TokenError",
    "actor_token_subject",
]

#: Issuer name that goes in every minted token's ``iss`` claim.
ISSUER = "https://auth.northstar.example"

#: How long a just-in-time credential lives. Seconds, deliberately.
DEFAULT_TTL_SECONDS = 60


class TokenError(RuntimeError):
    """Base class for every way a token can be refused.

    All of these are *permanent* failures. A caller that retries an
    audience mismatch is a caller that has misunderstood the problem, so
    the gateway reports them with ``retryable=False``.
    """


class InsufficientScope(TokenError):
    """The subject's grant does not contain the scope that was asked for.

    The correct response is step-up authorization: ask the user for the
    scope. It is never to widen the agent's own default grant, which is
    how a delegated task quietly becomes a service-authority task.
    """

    def __init__(self, scope: str) -> None:
        self.scope = scope
        super().__init__(f"subject does not hold scope {scope!r}")


class AudienceMismatch(TokenError):
    """A token minted for one service was presented to another.

    This is the confused-deputy attack, and the audience claim is the
    control that stops it. The check belongs in the *receiver*, which is
    why :meth:`AuthorizationServer.verify` takes the audience it expects
    rather than reading it out of the token.
    """

    def __init__(self, expected: str, actual: str) -> None:
        self.expected = expected
        self.actual = actual
        super().__init__(
            f"token audience {actual!r} is not {expected!r}"
        )


class ExpiredToken(TokenError):
    """The token is past its ``exp`` claim."""

    def __init__(self, expires_at: float, now: float) -> None:
        self.expires_at = expires_at
        self.now = now
        super().__init__(
            f"token expired at {expires_at:g}; it is now {now:g}"
        )


@dataclass(frozen=True)
class ScopedToken:
    """A short-lived, audience-bound, scoped credential.

    ``value`` is an opaque handle rather than a JWT. That is deliberate:
    an artifact that shipped a signing key would be shipping a secret, and
    every property the chapter cares about is in the claims.
    """

    value: str
    claims: dict[str, Any] = field(default_factory=dict)

    @property
    def subject(self) -> str:
        """The user this token acts for."""
        return str(self.claims.get("sub", ""))

    @property
    def actor(self) -> str:
        """The workload actually making the call."""
        act = self.claims.get("act") or {}
        return str(act.get("sub", "")) if isinstance(act, dict) else ""

    @property
    def audience(self) -> str:
        """The single service permitted to accept this token."""
        return str(self.claims.get("aud", ""))

    @property
    def scope(self) -> str:
        """The one permission this token conveys."""
        return str(self.claims.get("scope", ""))

    @property
    def expires_at(self) -> float:
        """Absolute expiry, from the ``exp`` claim."""
        return float(self.claims.get("exp", 0.0))

    def is_expired(self, now: float) -> bool:
        """Whether the token has aged out."""
        return now >= self.expires_at


def actor_token_subject(actor_token: str) -> str:
    """The workload named by an actor token.

    The agent's workload credential is ``workload:<name>@<version>`` here.
    In production this is a SPIFFE identity or a cloud workload identity,
    and the version is what makes "which build decided this" answerable.
    """
    return actor_token.removeprefix("workload:")


class AuthorizationServer:
    """Mints delegated tokens, and refuses to mint the ones it should not.

    Args:
        grants: Subject token to the scopes that subject actually holds.
            Nothing the agent says changes this mapping, which is the
            whole point of putting it outside the agent.
        clock: Injectable time source, so expiry is testable without
            sleeping.

    Example:
        >>> server = AuthorizationServer(
        ...     grants={"user:CUST-8841": frozenset({"refunds.write"})},
        ...     clock=lambda: 1000.0,
        ... )
        >>> token = server.sign(
        ...     sub="CUST-8841",
        ...     act={"sub": "northstar-support-agent@v1.8.0"},
        ...     aud="https://refunds.northstar.example",
        ...     scope="refunds.write",
        ...     resource="order:NR-2026-0041827",
        ...     ttl_s=60,
        ... )
        >>> token.claims["exp"]
        1060.0
    """

    def __init__(
        self,
        grants: dict[str, frozenset[str]] | None = None,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self.grants: dict[str, frozenset[str]] = dict(grants or {})
        self._clock: Callable[[], float] = clock or _monotonic_ticks()
        #: Append-only record of every token minted, for the demo to print.
        self.issued: list[ScopedToken] = []

    def now(self) -> float:
        """Current time, from the injected clock."""
        return self._clock()

    def grants_for(self, subject_token: str) -> frozenset[str]:
        """The scopes the subject genuinely holds.

        An unknown subject holds nothing. Failing closed on an unknown
        principal is cheaper than discovering later that a typo granted
        everything.
        """
        return self.grants.get(subject_token, frozenset())

    def subject_of(self, subject_token: str) -> str:
        """The user id inside a subject token."""
        return subject_token.removeprefix("user:")

    def sign(
        self,
        *,
        sub: str,
        act: dict[str, str],
        aud: str,
        scope: str,
        resource: str,
        ttl_s: int = DEFAULT_TTL_SECONDS,
    ) -> ScopedToken:
        """Mint one token. Read-only with respect to the world."""
        issued_at = self.now()
        claims: dict[str, Any] = {
            "iss": ISSUER,
            "sub": sub,
            "act": dict(act),
            "aud": aud,
            "scope": scope,
            "resource": resource,
            "exp": issued_at + ttl_s,
        }
        token = ScopedToken(
            value=f"tok_{short_hash(claims, 16)}",
            claims=claims,
        )
        self.issued.append(token)
        return token

    def verify(
        self,
        token: ScopedToken,
        *,
        audience: str,
        scope: str,
    ) -> ScopedToken:
        """Accept a token, or refuse it. Called by the *receiver*.

        Raises:
            AudienceMismatch: The token was minted for another service.
            ExpiredToken: The token is past its ``exp``.
            InsufficientScope: The token does not convey ``scope``.
        """
        if token.audience != audience:
            raise AudienceMismatch(audience, token.audience)
        now = self.now()
        if token.is_expired(now):
            raise ExpiredToken(token.expires_at, now)
        if token.scope != scope:
            raise InsufficientScope(scope)
        return token


def _monotonic_ticks() -> Callable[[], float]:
    """A deterministic clock: one second per read, starting at zero.

    Real time would make the expiry test flaky and the demo output
    unstable. A counter makes both reproducible, which is Chapter 21's
    rule about controlling the sources of nondeterminism that are yours.
    """
    state = {"t": 0.0}

    def tick() -> float:
        state["t"] += 1.0
        return state["t"]

    return tick
