"""A loopback stand-in for the network the sandbox must not reach.

This is the fixture the chapter argues about. A metadata-endpoint test
that tries to reach ``169.254.169.254`` from a laptop with no route to
link-local passes whether or not your policy exists, which makes it worse
than no test: it reports a control you do not have.

So the target is real. One HTTP server binds 127.0.0.1 on an ephemeral
port and serves two things: a stub instance-metadata document under the
test hostname ``metadata.test``, and an ordinary CSV under
``files.northstar.test``, which is the host an allowlist may name. The
policy resolves ``metadata.test`` to 127.0.0.1, which is in a blocked
range, and denies. A pass means the policy denied, not that the network
was unavailable.

Nothing here is a credential. The metadata document holds obvious
placeholders, because a fixture that ships something that looks like a
key teaches the wrong reflex.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from types import TracebackType

from egress import EgressPolicy, StaticResolver
from netshim import NetworkConfig

__all__ = ["METADATA_HOST", "PUBLIC_HOST", "REBIND_HOST", "StubNetwork"]

METADATA_HOST = "metadata.test"
PUBLIC_HOST = "files.northstar.test"

#: One permitted answer and one private one. A first-answer check waves
#: this through; the policy checks every answer and does not.
REBIND_HOST = "rebind.test"

#: TEST-NET-3 (RFC 5737). Public, documentation-only, and outside every
#: blocked range, which is exactly what an allowlisted host needs to be.
PUBLIC_ADDR = "203.0.113.10"
REBIND_ADDRS = ("203.0.113.20", "10.0.0.5")

METADATA_PATH = "/latest/meta-data/iam/security-credentials/northstar-task"
PUBLIC_PATH = "/refunds-2026-06.csv"

STUB_CREDENTIALS = {
    "Code": "Success",
    "Type": "AWS-HMAC",
    "AccessKeyId": "EXAMPLE-NOT-A-REAL-KEY",
    "SecretAccessKey": "EXAMPLE-NOT-A-REAL-SECRET",
    "Token": "EXAMPLE-NOT-A-REAL-TOKEN",
    "Expiration": "2026-07-30T00:00:00Z",
}

PUBLIC_BODY = "order_id,amount_cents\nNR-2026-0041827,3250\n"


class _Handler(BaseHTTPRequestHandler):
    """Serves the two documents and says nothing to stderr."""

    def do_GET(self) -> None:  # noqa: N802 - http.server's spelling
        """Answer the metadata path and the CSV path; 404 anything else."""
        if self.path.startswith(METADATA_PATH):
            body = json.dumps(STUB_CREDENTIALS).encode()
            content_type = "application/json"
        elif self.path.startswith(PUBLIC_PATH):
            body = PUBLIC_BODY.encode()
            content_type = "text/csv"
        else:
            self.send_error(404, "no such stub document")
            return
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        """Silence. The test's output is the assertion, not the access log."""
        return None


class StubNetwork:
    """The loopback server plus the resolver table and routes it implies."""

    def __init__(self) -> None:
        """Bind an ephemeral loopback port and start serving."""
        self._server = HTTPServer(("127.0.0.1", 0), _Handler)
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            name="ch12-stub-network",
            daemon=True,
        )
        self._thread.start()

    @property
    def port(self) -> int:
        """The ephemeral port the stub is listening on."""
        return int(self._server.server_address[1])

    @property
    def metadata_url(self) -> str:
        """The URL the row-41 payload asks for. Denied by the policy."""
        return f"https://{METADATA_HOST}{METADATA_PATH}"

    @property
    def public_url(self) -> str:
        """A URL an allowlist may name. Allowed only if it names the host."""
        return f"https://{PUBLIC_HOST}{PUBLIC_PATH}"

    def resolver(self) -> StaticResolver:
        """The offline stand-in for DNS, wired to this server."""
        return StaticResolver(
            {
                METADATA_HOST: ["127.0.0.1"],
                PUBLIC_HOST: [PUBLIC_ADDR],
                REBIND_HOST: list(REBIND_ADDRS),
            }
        )

    def routes(self) -> dict[str, tuple[str, int]]:
        """Where an allowed host actually lives while the suite is offline."""
        here = ("127.0.0.1", self.port)
        return {METADATA_HOST: here, PUBLIC_HOST: here, REBIND_HOST: here}

    def network(
        self,
        allow_hosts: frozenset[str] = frozenset(),
    ) -> NetworkConfig:
        """A :class:`NetworkConfig` for this server. Default: no egress."""
        return NetworkConfig(
            policy=EgressPolicy(allow_hosts, resolver=self.resolver()),
            routes=self.routes(),
        )

    def close(self) -> None:
        """Stop serving and close the listening socket."""
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)

    def __enter__(self) -> StubNetwork:
        """Support ``with StubNetwork() as net:``."""
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        """Always close, so no socket outlives the test that opened it."""
        self.close()
