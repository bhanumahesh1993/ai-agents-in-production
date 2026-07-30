"""The Northstar read server, over both transports, with real token checks.

    python artifacts/ch09-mcp/demo.py            # everything
    ./run.sh stdio                               # the local pipe
    ./run.sh http                                # one endpoint, token required
    ./run.sh http --no-token                     # 401 plus the discovery hint

One ``ToolRegistry``, one ``ReadServer`` object, two transports rendering
it. The demo walks the full authorization flow -- unauthenticated call, 401
with a ``WWW-Authenticate`` pointing at protected-resource metadata, client
fetches it, discovers the authorization server, obtains an audience-bound
token, retries -- then runs the negative cases, compares what the two
transports advertise, and finishes against a server whose tool description
has drifted from its pin.

Exits non-zero if the unauthenticated call is not refused, if a token for
the wrong audience or the wrong issuer or without ``orders.read`` is
accepted, if the two transports disagree about the tool surface, if a
server this client cannot speak to is admitted, or if the drifted server is
accepted against its pin.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from dataclasses import dataclass
from typing import Any

from client.negotiate import (
    SUPPORTED,
    MissingCapability,
    UnsupportedRevision,
    negotiate,
)
from client.pins import PINS, PinMismatch, check_pin, surface_hash
from client.session import (
    HttpRefused,
    HttpTransport,
    McpSession,
    StdioTransport,
)
from northstar_contracts import World
from northstar_runtime import ToolRegistry
from server.auth import (
    ISSUER,
    READ_SCOPE,
    RESOURCE,
    RESOURCE_ORIGIN,
    InsufficientScope,
    Unauthorized,
    principal_for,
)
from server.authserver import AuthorizationServer, ClientMetadataHost, TokenRequest
from server.drift import VendorCatalogServer
from server.expose import ConfigError, descriptors, read_only_registry
from server.readserver import PREVIOUS_REVISION, ReadServer
from server.transports import MCP_PATH, Fabric, HttpEndpoint, StdioPipe

ORDER = "NR-2026-0041827"        # US$84.00, delivered, two items
SKU = "NR-LAMPSHADE-03"          # the lamp shade, 3,250 cents
ORDER_TOTAL_CENTS = 8400
SHADE_CENTS = 3250
APPROVAL_THRESHOLD_CENTS = 5000

MCP_URL = f"{RESOURCE_ORIGIN}{MCP_PATH}"
ROGUE_ISSUER = "https://auth.notnorthstar.example"
OTHER_RESOURCE = "https://mcp.vendor.example/catalog"
CLIENT_ID = "https://apps.northstar.example/clients/support"

# Capabilities this integration depends on. ``resources`` is in the list on
# purpose: the refund policy corpus belongs behind resources, not behind
# forty tools, and a server that cannot serve it is one we refuse early.
NEEDED = {"tools", "resources"}


@dataclass
class Deployment:
    """Everything wired together, with no socket anywhere in it."""

    world: World
    registry: ToolRegistry
    server: ReadServer
    endpoint: HttpEndpoint
    fabric: Fabric
    authority: AuthorizationServer
    rogue: AuthorizationServer
    vendor: VendorCatalogServer


def wire() -> Deployment:
    """Stand up the read server, the identity provider, and the vendor."""
    world = World()
    registry = read_only_registry(world)
    server = ReadServer(registry)
    endpoint = HttpEndpoint(server)

    fabric = Fabric()
    fabric.mount(RESOURCE_ORIGIN, endpoint)
    fabric.mount("https://apps.northstar.example", ClientMetadataHost({
        "/clients/support": {
            "client_id": CLIENT_ID,
            "client_name": "Northstar support agent",
            "redirect_uris": ["https://apps.northstar.example/callback"],
        },
    }))
    authority = AuthorizationServer(issuer=ISSUER, fabric=fabric)
    rogue = AuthorizationServer(issuer=ROGUE_ISSUER, fabric=fabric)
    fabric.mount(ISSUER, authority)
    fabric.mount(ROGUE_ISSUER, rogue)

    vendor = VendorCatalogServer(drift_on_connection=3)
    return Deployment(world, registry, server, endpoint, fabric,
                      authority, rogue, vendor)


# ---------------------------------------------------------------- connecting


def stdio_session(server: ReadServer) -> McpSession:
    """A session over the local pipe. No token: the launch was the grant."""
    return McpSession(StdioTransport(StdioPipe(server)), server.name)


def http_session(
    dep: Deployment,
    **kwargs: Any,
) -> tuple[McpSession, HttpTransport]:
    """A session over the single HTTP endpoint, with the discovery walk."""
    transport = HttpTransport(dep.fabric, MCP_URL, client_id=CLIENT_ID,
                              **kwargs)
    return McpSession(transport), transport


def read_three_tools(session: McpSession) -> dict[str, Any]:
    """Call all three read tools and return what came back."""
    return {
        "get_order": session.call_tool("get_order", {"order_id": ORDER}),
        "get_policy": session.call_tool(
            "get_policy", {"reason": "damaged", "sku": SKU}
        ),
        "search_orders": session.call_tool(
            "search_orders", {"status": "delivered", "page_size": 2}
        ),
    }


# -------------------------------------------------------------------- checks


def show_surface(dep: Deployment, failures: list[str]) -> None:
    """The surface is derived from the registry, and writes are refused."""
    banner("the tool surface is derived, and a write tool is a build error")
    for tool in descriptors(dep.registry):
        print(f"  {tool['name']:<14} {tool['description'][:52]}...")

    mixed = read_only_registry(dep.world)
    write_spec, write_fn = next(
        (s, f) for s, f in dep.world.tools() if s.writes
    )
    mixed.register(write_spec, write_fn)
    try:
        ReadServer(mixed, name="northstar-reads-misconfigured")
    except ConfigError as exc:
        print(f"\n  registering {write_spec.name} -> ConfigError: {exc}")
        print("  the server refuses to start; the tool is not reachable")
    else:
        failures.append("a write tool was accepted on the read server")


def run_stdio(dep: Deployment, failures: list[str]) -> list[dict]:
    """Negotiate and call over the local pipe."""
    banner("stdio: a child process, and no authorization step at all")
    session = stdio_session(dep.server)
    revision = negotiate(session, NEEDED)
    print(f"  negotiated revision : {revision}")
    print(f"  declared capabilities: {sorted(session.capabilities)}")

    tools = session.list_tools()
    print(f"  tool surface        : {surface_hash(tools)}")

    results = read_three_tools(session)
    print(f"  get_order           : total {results['get_order']['total_cents']}"
          f" cents, {len(results['get_order']['items'])} items")
    print("  get_policy          : threshold "
          f"{results['get_policy']['approval_threshold_cents']} cents")
    print("  search_orders       : "
          f"{results['search_orders']['total_matches']} matches")
    check_amounts(results, "stdio", failures)
    print("\n  note: the principal here came from the launch, not a token.")
    print("  a stdio server also inherits this process's environment.")
    return tools


def run_unauthenticated(dep: Deployment, failures: list[str]) -> str:
    """The call that has to be refused, and refused usefully."""
    banner("http --no-token: the 401 that makes configuration unnecessary")
    session, _ = http_session(dep, discover=False)
    try:
        session.initialize(SUPPORTED[0])
    except HttpRefused as exc:
        print(f"  HTTP {exc.status}")
        for part in exc.challenge.split(", "):
            print(f"    {part}")
        if exc.status != 401 or "resource_metadata=" not in exc.challenge:
            failures.append("the 401 carried no protected-resource pointer")
        return exc.challenge
    failures.append("the unauthenticated call was not refused")
    print("  FAILED: the call succeeded without a token")
    return ""


def run_http(dep: Deployment, failures: list[str]) -> list[dict]:
    """The full walk: 401, metadata, issuer, token, retry."""
    banner("http: one endpoint, discovery from the resource outward")
    session, transport = http_session(dep)
    revision = negotiate(session, NEEDED)
    for step in transport.walk:
        print(f"  {step}")
    print(f"\n  negotiated revision : {revision}")
    print(f"  declared capabilities: {sorted(session.capabilities)}")
    print(f"  session id          : {transport.session_id}")

    tools = session.list_tools()
    print(f"  tool surface        : {surface_hash(tools)}")

    results = read_three_tools(session)
    print(f"  get_order           : total {results['get_order']['total_cents']}"
          f" cents, {len(results['get_order']['items'])} items")
    print("  get_policy          : threshold "
          f"{results['get_policy']['approval_threshold_cents']} cents")
    print("  search_orders       : "
          f"{results['search_orders']['total_matches']} matches")
    for note in transport.streamed:
        print(f"  stream notification : {note['params']['message']}")
    check_amounts(results, "http", failures)
    return tools


def check_amounts(
    results: dict[str, Any],
    label: str,
    failures: list[str],
) -> None:
    """Integer cents, read off the world rather than off a transcript."""
    order = results["get_order"]
    shade = next(
        (i for i in order["items"] if i["sku"] == SKU), {"unit_price_cents": 0}
    )
    policy = results["get_policy"]
    if order["total_cents"] != ORDER_TOTAL_CENTS:
        failures.append(f"{label}: order total was {order['total_cents']}")
    if shade["unit_price_cents"] != SHADE_CENTS:
        failures.append(f"{label}: lamp shade was {shade['unit_price_cents']}")
    if policy["approval_threshold_cents"] != APPROVAL_THRESHOLD_CENTS:
        failures.append(f"{label}: threshold was "
                        f"{policy['approval_threshold_cents']}")


def compare_transports(
    over_stdio: list[dict],
    over_http: list[dict],
    failures: list[str],
) -> None:
    """One server object, two renderings, one answer."""
    banner("the two transports render the same server object")
    left, right = surface_hash(over_stdio), surface_hash(over_http)
    print(f"  stdio : {left}")
    print(f"  http  : {right}")
    if over_stdio != over_http:
        failures.append("the two transports advertise different tools")
        print("  FAILED: the descriptors differ")
    else:
        print("  identical, because neither one hand-maintains a copy")


def run_negatives(dep: Deployment, failures: list[str]) -> None:
    """Three tokens that must each be refused, for three distinct reasons."""
    banner("the negative cases, which are what you are actually asserting")

    wrong_audience = dep.authority.issue_token(TokenRequest(
        client_id=CLIENT_ID, subject="CUST-8841",
        resource=OTHER_RESOURCE, scope=READ_SCOPE,
    ))
    wrong_issuer = dep.rogue.issue_token(TokenRequest(
        client_id=CLIENT_ID, subject="CUST-8841",
        resource=RESOURCE, scope=READ_SCOPE,
    ))
    no_scope = dep.authority.issue_token(TokenRequest(
        client_id=CLIENT_ID, subject="CUST-8841",
        resource=RESOURCE, scope="profile.read",
    ))

    cases = [
        ("wrong audience", wrong_audience, 401),
        ("wrong issuer", wrong_issuer, 401),
        ("missing orders.read", no_scope, 403),
    ]
    for label, token, expected_status in cases:
        session, _ = http_session(dep, token=token)
        try:
            session.initialize(SUPPORTED[0])
        except HttpRefused as exc:
            body = exc.body if isinstance(exc.body, dict) else {}
            print(f"  {label:<20} -> HTTP {exc.status} "
                  f"{body.get('error_description', '')}")
            if exc.status != expected_status:
                failures.append(
                    f"{label}: expected {expected_status}, got {exc.status}"
                )
            if expected_status == 403 and f'scope="{READ_SCOPE}"' not in exc.challenge:
                failures.append(f"{label}: no step-up scope in the challenge")
        else:
            failures.append(f"{label}: the token was accepted")
            print(f"  {label:<20} -> ACCEPTED, which is the bug")

    try:
        principal_for(no_scope, READ_SCOPE)
    except InsufficientScope as exc:
        print(f"\n  step-up hint        : {exc.step_up()}")
    except Unauthorized:
        failures.append("missing scope was reported as an authn failure")


def run_admission(dep: Deployment, failures: list[str]) -> None:
    """Revision and capability are negotiated separately. Check both."""
    banner("admission: the revision is not the capability")

    old = ReadServer(read_only_registry(dep.world), name="northstar-legacy",
                     revision="2025-03-26")
    try:
        negotiate(stdio_session(old), NEEDED)
    except UnsupportedRevision as exc:
        print(f"  revision refused    : {exc}")
    else:
        failures.append("a server on an unsupported revision was admitted")

    thin = ReadServer(
        read_only_registry(dep.world), name="northstar-thin",
        revision=PREVIOUS_REVISION,
        capabilities={"tools": {"listChanged": True}},
    )
    try:
        negotiate(stdio_session(thin), NEEDED)
    except MissingCapability as exc:
        print(f"  revision {PREVIOUS_REVISION} is supported, and: {exc}")
        print("  refused at connect time, not on turn 40")
    else:
        failures.append("a server missing a needed capability was admitted")


def run_pins(dep: Deployment, failures: list[str]) -> None:
    """Three connections to a vendor server. The third one has moved."""
    banner("the description pin: connect three times, fail on the third")

    for attempt in (1, 2, 3):
        session = stdio_session(dep.vendor)
        session.initialize(SUPPORTED[0])
        tools = session.list_tools()
        try:
            digest = check_pin(dep.vendor.name, tools)
        except PinMismatch as exc:
            print(f"  connection {attempt}        : REFUSED")
            print(f"    pinned  {exc.expected}")
            print(f"    served  {exc.actual}")
            for line in exc.diff:
                print(f"    {line}")
            print("    the new text asks the model to read "
                  "~/.aws/credentials")
            if attempt != 3:
                failures.append(f"pin failed early, on connection {attempt}")
            break
        print(f"  connection {attempt}        : matches pin {digest}")
        if attempt == 3:
            failures.append("the drifted tool surface was accepted")
            print("  FAILED: the description changed and the pin did not")

    session = stdio_session(dep.server)
    session.initialize(SUPPORTED[0])
    try:
        check_pin(dep.server.name, session.list_tools())
    except PinMismatch as exc:
        failures.append(f"the read server no longer matches its pin: {exc}")
    else:
        print(f"  northstar-reads     : still {PINS['northstar-reads']}")


# ---------------------------------------------------------------------- main


def banner(title: str) -> None:
    """Print a section header."""
    print(f"\n=== {title} ===")


def main(argv: list[str] | None = None) -> int:
    """Run the mode asked for and return a process exit code."""
    args = list(sys.argv[1:] if argv is None else argv)
    no_token = "--no-token" in args
    positional = [a for a in args if not a.startswith("-")]
    mode = positional[0] if positional else "all"
    if mode not in ("all", "stdio", "http"):
        print(f"usage: demo.py [all|stdio|http] [--no-token]; got {mode!r}")
        return 2

    dep = wire()
    failures: list[str] = []
    over_stdio: list[dict] = []
    over_http: list[dict] = []

    if mode in ("all", "stdio"):
        show_surface(dep, failures)
        over_stdio = run_stdio(dep, failures)
    if mode == "http" and no_token:
        run_unauthenticated(dep, failures)
    elif mode in ("all", "http"):
        run_unauthenticated(dep, failures)
        over_http = run_http(dep, failures)
    if mode == "all":
        compare_transports(over_stdio, over_http, failures)
        run_negatives(dep, failures)
        run_admission(dep, failures)
        run_pins(dep, failures)
        print("\n--- what this proves ---")
        print("An auditor can read the negotiated revision and capability")
        print("set of every session, the issuer and audience of the token")
        print("that authorized each call, and a byte-level record of the")
        print("descriptions the model was shown. Nothing here reached a")
        print(f"network: {len(dep.fabric.log)} requests, all in-process.")

    if failures:
        print("\nFAILED:")
        for line in failures:
            print(f"  - {line}")
        return 1
    print("\nOK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
