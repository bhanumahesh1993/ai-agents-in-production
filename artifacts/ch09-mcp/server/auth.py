"""Token validation for an MCP server that is an OAuth 2.1 protected resource.

An MCP server is not an authorization server. It does not mint tokens, it
does not hold anybody's password, and it does not accept a credential that
was issued for something else. It validates a token and enforces scope.

Four checks, in order, all of them fail-closed:

1. signature and expiry -- is this token intact and still alive?
2. issuer               -- did the identity provider we trust mint it?
3. audience             -- was it minted for *this* resource?
4. scope                -- does it carry the permission this call needs?

Check 3 is the one that does the security work. Without audience binding, a
token minted for one server is replayable against another, which is the
confused-deputy pattern and is trivially available to anyone who can get an
agent to talk to a server they control.

Two deliberate deviations from the chapter's excerpt, both so the artifact
runs offline with no configuration:

* ``RESOURCE`` and ``ISSUER`` are module constants here. The chapter reads
  them from the environment, which is right in production and unhelpful in
  a demo that has to run on a machine that has never heard of Northstar.
* The token is an HMAC over canonical JSON signed with an obviously fake key
  defined a few lines below, not a JWT. No JWT library, no key material, no
  environment lookup. The four checks are the part worth copying; the
  encoding is not.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import time
from typing import Any

from northstar_contracts import canonical_json
from northstar_policy import Principal

__all__ = [
    "ISSUER",
    "READ_SCOPE",
    "RESOURCE",
    "RESOURCE_METADATA_PATH",
    "RESOURCE_METADATA_URL",
    "RESOURCE_ORIGIN",
    "InsufficientScope",
    "Unauthorized",
    "launch_principal",
    "principal_for",
    "protected_resource_metadata",
    "sign_claims",
    "verify_signed_token",
]

# The resource identifier. A token's audience must contain this exact
# string or the token was not issued for us, whoever signed it.
RESOURCE_ORIGIN = "https://mcp.northstar.example"
RESOURCE = f"{RESOURCE_ORIGIN}/reads"

# The one authorization server we trust to mint tokens for that resource.
ISSUER = "https://auth.northstar.example"

# One scope. This server reads orders; it holds no credential for anything
# downstream and cannot be talked into writing.
READ_SCOPE = "orders.read"

RESOURCE_METADATA_PATH = "/.well-known/oauth-protected-resource"
RESOURCE_METADATA_URL = f"{RESOURCE_ORIGIN}{RESOURCE_METADATA_PATH}"

# Not a secret. It is checked into a public book repository on purpose, so
# that nothing in this artifact reads a key from the environment or asks you
# for one. Real deployments verify an asymmetric signature against a key set
# fetched from the issuer; the shape of the check is the same.
TEST_SIGNING_KEY = b"ch09-mock-mcp-signing-key-not-a-secret"


class Unauthorized(Exception):
    """The token does not authenticate a caller of *this* server.

    Carries a machine-readable ``reason`` so the transport can render a
    distinct ``WWW-Authenticate`` header per failure. "Wrong audience" and
    "expired" are different operational problems with different owners.
    """

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class InsufficientScope(Exception):
    """Authenticated, but the token does not carry the scope required.

    This is a 403, not a 401, and it is not a dead end: the client runs an
    incremental authorization to obtain the named scope and retries. That
    is step-up, and it is how an agent escalates legitimately instead of
    carrying maximum privilege from the first turn.
    """

    def __init__(self, required: str, held: frozenset[str] = frozenset()) -> None:
        super().__init__(f"requires scope {required!r}")
        self.required = required
        self.held = held

    def step_up(self) -> dict[str, str]:
        """The hint a client needs to ask for exactly one more scope."""
        return {
            "error": "insufficient_scope",
            "scope": self.required,
            "resource_metadata": RESOURCE_METADATA_URL,
        }


def sign_claims(claims: dict[str, Any]) -> str:
    """Encode and sign a claim set. Only the mock issuer calls this.

    Args:
        claims: The claim set. ``iss``, ``aud``, ``sub``, ``scope``, ``exp``.

    Returns:
        ``<base64url(canonical json)>.<hmac>``.
    """
    body = base64.urlsafe_b64encode(
        canonical_json(claims).encode("utf-8")
    ).decode("ascii").rstrip("=")
    return f"{body}.{_mac(body)}"


def verify_signed_token(token: str) -> dict[str, Any]:
    """Check the signature and the expiry. The first of the four checks.

    Args:
        token: The bearer token as presented.

    Returns:
        The claim set, once it is known to be intact and unexpired.

    Raises:
        Unauthorized: Malformed, badly signed, or expired.
    """
    body, _, mac = token.partition(".")
    if not body or not mac:
        raise Unauthorized("malformed token")
    if not hmac.compare_digest(mac, _mac(body)):
        raise Unauthorized("bad token signature")

    padded = body + "=" * (-len(body) % 4)
    try:
        raw = base64.urlsafe_b64decode(padded.encode("ascii"))
        claims = json.loads(raw.decode("utf-8"))
    except (binascii.Error, UnicodeDecodeError, ValueError) as exc:
        raise Unauthorized("malformed token") from exc
    if not isinstance(claims, dict):
        raise Unauthorized("malformed token")

    if float(claims.get("exp", 0)) <= time.time():
        raise Unauthorized("token expired")
    return claims


def principal_for(token: str, required: str) -> Principal:
    """Validate the token, then bind a scoped principal.

    Args:
        token: The bearer token from the ``Authorization`` header.
        required: The scope this call needs, for example ``orders.read``.

    Returns:
        The principal the call runs as. Three identities, not one: the
        human, the agent, and the team accountable for the agent.

    Raises:
        Unauthorized: Signature, expiry, issuer, or audience failed.
        InsufficientScope: Authenticated, but missing ``required``.
    """
    claims = verify_signed_token(token)          # signature + exp

    if claims.get("iss") != ISSUER:
        raise Unauthorized("issuer mismatch")

    audience = claims.get("aud", [])
    if isinstance(audience, str):
        audience = [audience]
    if RESOURCE not in audience:
        raise Unauthorized("token not issued for this server")

    scopes = frozenset(str(claims.get("scope", "")).split())
    if required not in scopes:
        raise InsufficientScope(required, scopes)  # triggers step-up

    return Principal(
        user_id=str(claims.get("sub", "")) or None,
        agent_id=str(claims.get("azp", "unknown-client")),
        operator_id="northstar-support",
        scopes=scopes,
    )


def protected_resource_metadata() -> dict[str, Any]:
    """The document the 401 points at, served at the well-known path.

    This is the whole reason a client needs no configuration: it learns the
    resource identifier to bind its token to, and which authorization
    servers can mint one, by asking the resource itself.
    """
    return {
        "resource": RESOURCE,
        "authorization_servers": [ISSUER],
        "scopes_supported": [READ_SCOPE],
        "bearer_methods_supported": ["header"],
        "resource_documentation": f"{RESOURCE_ORIGIN}/docs",
    }


def launch_principal() -> Principal:
    """The principal a stdio connection runs as.

    stdio has no authorization step, because the transport's entire trust
    model is "you launched it". The server is a child process running as
    the same operating-system user as the client, holding whatever that
    process holds. So there is no token to validate and nothing to bind:
    the principal comes from the launch, and the scope is granted by the
    fact that somebody could spawn the process at all.

    That is exactly right on a laptop and wrong in a shared runtime, which
    is the point of the comparison the demo prints.
    """
    return Principal(
        user_id="local-operator",
        agent_id="northstar-support-agent",
        operator_id="northstar-support",
        scopes=frozenset({READ_SCOPE}),
    )


def _mac(body: str) -> str:
    """HMAC-SHA256 over the encoded body, truncated for readability."""
    return hmac.new(
        TEST_SIGNING_KEY, body.encode("ascii"), hashlib.sha256
    ).hexdigest()[:32]
