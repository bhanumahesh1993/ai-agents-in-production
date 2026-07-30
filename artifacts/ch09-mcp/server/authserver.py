"""A mock OAuth 2.1 authorization server, and the documents it publishes.

This is the party the MCP server is *not*. It mints tokens; the resource
server validates them. Keeping the two apart in the code is the point: the
read server has no branch that could ever produce a token, so no argument,
prompt, or description can talk it into producing one.

Deliberately not modelled, and named here rather than faked: the
interactive authorization-code leg. There is no browser redirect, no PKCE
challenge, no consent screen, and no refresh token. Those matter and they
are orthogonal to what Chapter 9 is demonstrating, which is discovery,
audience binding, issuer validation, and scope. The token endpoint here
mints what it is asked for so that the negative cases -- a token for
another audience, a token from another issuer -- are constructible in a
few lines.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

from server.auth import ISSUER, READ_SCOPE, sign_claims
from server.transports import Fabric, HttpRequest, HttpResponse

__all__ = [
    "AS_METADATA_PATH",
    "OIDC_METADATA_PATH",
    "AuthorizationServer",
    "ClientMetadataHost",
    "TokenRequest",
]

AS_METADATA_PATH = "/.well-known/oauth-authorization-server"
OIDC_METADATA_PATH = "/.well-known/openid-configuration"
TOKEN_PATH = "/token"


@dataclass(frozen=True)
class TokenRequest:
    """What a client asks the token endpoint for."""

    client_id: str
    subject: str
    resource: str
    scope: str
    lifetime_s: int = 300


class ClientMetadataHost:
    """Serves a Client ID Metadata Document at a URL the client controls.

    Dynamic client registration gave the authorization server an unbounded
    pile of anonymous records and a ``client_id`` that attested to nothing.
    A CIMD ``client_id`` is a URL that resolves to a document the client
    publishes about itself, so the identifier is a name under someone's
    domain control: allowlistable, blockable, and still meaningful in an
    audit record six months later.

    The cost is visible right here: the authorization server becomes an
    HTTP client fetching a URL an attacker can influence. That is a
    server-side request forgery surface, which is why ``AuthorizationServer``
    below fetches only from an allowlist of hosts.
    """

    def __init__(self, documents: dict[str, dict[str, Any]]) -> None:
        self.documents = documents

    def handle(self, request: HttpRequest) -> HttpResponse:
        """Serve one client metadata document, by path."""
        document = self.documents.get(request.path)
        if document is None:
            return HttpResponse(404, body={"error": "not_found"})
        return HttpResponse(
            200, {"Content-Type": "application/json"}, document
        )


@dataclass
class AuthorizationServer:
    """The mock identity provider. Mints audience-bound bearer tokens.

    Args:
        issuer: Its issuer identifier, which is also its origin. Construct a
            second one with a different issuer to get the wrong-issuer case.
        fabric: Set to resolve CIMD ``client_id`` URLs. Left unset, any
            client identifier is accepted, which is the pre-CIMD world.
        allowed_client_hosts: Hosts whose client metadata documents this
            server will fetch. The SSRF allowlist.
    """

    issuer: str = ISSUER
    fabric: Fabric | None = None
    allowed_client_hosts: frozenset[str] = frozenset({"apps.northstar.example"})
    issued: list[dict[str, Any]] = field(default_factory=list)

    def metadata(self) -> dict[str, Any]:
        """Authorization server metadata, found from the issuer."""
        return {
            "issuer": self.issuer,
            "token_endpoint": f"{self.issuer}{TOKEN_PATH}",
            "authorization_endpoint": f"{self.issuer}/authorize",
            "scopes_supported": [READ_SCOPE, "refunds.write"],
            "response_types_supported": ["code"],
            "code_challenge_methods_supported": ["S256"],
            "client_id_metadata_document_supported": True,
            "authorization_response_iss_parameter_supported": True,
        }

    def issue_token(self, request: TokenRequest) -> str:
        """Mint one token, bound to the resource the client named.

        The audience is the ``resource`` the client asked for, per RFC 8707.
        That is what lets the resource server reject a token minted for
        somebody else without needing to know who else exists.
        """
        now = time.time()
        claims = {
            "iss": self.issuer,
            "sub": request.subject,
            "aud": [request.resource],
            "azp": request.client_id,
            "scope": request.scope,
            "iat": int(now),
            "exp": now + request.lifetime_s,
        }
        self.issued.append(claims)
        return sign_claims(claims)

    def handle(self, request: HttpRequest) -> HttpResponse:
        """Serve discovery, or the token endpoint."""
        if request.path in (AS_METADATA_PATH, OIDC_METADATA_PATH):
            return HttpResponse(
                200, {"Content-Type": "application/json"}, self.metadata()
            )
        if request.path != TOKEN_PATH:
            return HttpResponse(404, body={"error": "not_found"})
        if request.method != "POST":
            return HttpResponse(405, body={"error": "method_not_allowed"})

        body: dict[str, Any] = request.body or {}
        client_id = str(body.get("client_id", ""))
        problem = self._check_client(client_id)
        if problem is not None:
            return HttpResponse(
                400, body={"error": "invalid_client", "error_description": problem}
            )

        resource = str(body.get("resource", ""))
        if not resource:
            # RFC 8707 is not optional here. A token with no audience is a
            # token that works everywhere, which is the bug.
            return HttpResponse(
                400,
                body={"error": "invalid_target",
                      "error_description": "resource indicator required"},
            )

        token = self.issue_token(TokenRequest(
            client_id=client_id,
            subject=str(body.get("subject", "CUST-8841")),
            resource=resource,
            scope=str(body.get("scope", READ_SCOPE)),
            lifetime_s=int(body.get("lifetime_s", 300)),
        ))
        return HttpResponse(200, {"Content-Type": "application/json"}, {
            "access_token": token,
            "token_type": "Bearer",
            "expires_in": int(body.get("lifetime_s", 300)),
            "scope": str(body.get("scope", READ_SCOPE)),
            # RFC 9207. The client compares this against the issuer it
            # started discovery from, which is what closes the mix-up
            # attack that metadata discovery otherwise opens.
            "iss": self.issuer,
        })

    def _check_client(self, client_id: str) -> str | None:
        """Resolve a CIMD client identifier, or explain why not.

        Returns:
            ``None`` when the client is acceptable, else the reason.
        """
        if not client_id:
            return "client_id required"
        if self.fabric is None or not client_id.startswith("https://"):
            return None
        host = urlsplit(client_id).netloc
        if host not in self.allowed_client_hosts:
            return f"client_id host {host!r} is not allowlisted"
        response = self.fabric.fetch("GET", client_id)
        document = response.body if isinstance(response.body, dict) else {}
        if response.status != 200:
            return f"client metadata document not resolvable ({response.status})"
        if document.get("client_id") != client_id:
            return "client metadata document does not name its own client_id"
        return None
