"""What a delegated token's claims look like once decoded.

The point of RFC 8693 token exchange, for an audit trail, is this shape:
``sub`` names the user the action is taken for and ``act`` names the
workload actually taking it. Both facts arrive inside the credential, so
they land in the *target service's* own logs rather than depending on the
agent to report itself accurately.

Nothing here is a real token. There is no signing key in this repository
and no network call to obtain one; :mod:`authz_server` mints an opaque
handle and keeps the claims beside it in memory.
"""

from __future__ import annotations

from typing import Any

__all__ = ["CLAIMS", "REQUIRED_CLAIMS", "describe", "missing_claims"]

# artifacts/ch19-identity/claims.py (decoded, illustrative)
CLAIMS: dict[str, object] = {
    "iss": "https://auth.northstar.example",
    "sub": "CUST-8841",                 # the user acted for
    "act": {"sub": "northstar-support-agent@v1.8.0"},
    "aud": "https://refunds.northstar.example",
    "scope": "refunds.write",
    "resource": "order:NR-2026-0041827",
    "exp": 1785312060,                  # 60 seconds after issue
}

#: The claims an agent credential has to carry for the audit trail to
#: answer "who was this for, and which build did it". A token missing any
#: of them is a token that cannot be attributed, which is the failure the
#: chapter opens with.
REQUIRED_CLAIMS: tuple[str, ...] = (
    "iss",
    "sub",
    "act",
    "aud",
    "scope",
    "resource",
    "exp",
)


def missing_claims(claims: dict[str, Any]) -> list[str]:
    """Return the required claims ``claims`` does not carry."""
    return [name for name in REQUIRED_CLAIMS if name not in claims]


def describe(claims: dict[str, Any]) -> list[str]:
    """Render decoded claims as aligned lines, for the demo's output."""
    width = max(len(k) for k in claims)
    return [f"  {k.ljust(width)} : {claims[k]!r}" for k in claims]
