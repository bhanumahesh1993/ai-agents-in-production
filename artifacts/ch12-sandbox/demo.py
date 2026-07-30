"""Replay the chapter's row-41 payload through every rung of the ladder.

    python artifacts/ch12-sandbox/demo.py

The payload reads a metadata service and prints what it found. The
metadata service is real, running on loopback, and reachable, so a rung
that reports nothing is a rung whose policy denied rather than a rung
with no route. The in-process variant is included and clearly labelled as
the negative control: it is the only one that succeeds.

Exits non-zero if any isolated rung fails to deny the read, or if the
negative control fails to make it. That inversion is the whole point: a
suite in which nothing can reach the target proves nothing.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import sys

from broker import ScopeNotHeld, SecretsBroker, looks_like_secret, scrub_env
from egress import EgressPolicy
from ladder import CONTROL, build_ladder, close_all
from netshim import NetworkConfig
from northstar_policy import Decision, Principal
from sandbox import Sandbox
from stub_network import (
    METADATA_HOST,
    PUBLIC_HOST,
    REBIND_HOST,
    StubNetwork,
)
from subproc import SubprocessSandbox
from tool import SANDBOX_PRINCIPAL, run_code

# The nine lines from row 41, as a benign local stand-in. The original
# posted what it read to a host that did not belong to Northstar; that
# step is not reproduced here, because it is not the step the sandbox has
# to stop. The read is.
ROW_41_PAYLOAD = '''\
import urllib.request as u

with u.urlopen("{url}", timeout=2) as response:
    body = response.read().decode()
print("CREDENTIALS-READ:", body[:48])

rows = [1250, 3250, 899]          # the currency normalisation it promised
print("total_cents:", sum(rows))
'''

READ_SENTINEL = "CREDENTIALS-READ:"

# A long-lived credential lives outside the boundary, in the broker, and
# this string is a placeholder for one. Nothing here is a real secret.
LONG_LIVED_CREDENTIAL = "EXAMPLE-northstar-orders-service-credential"


def row_41_run(box: Sandbox, payload: str) -> tuple[bool, dict]:
    """Run the payload through the tool. Returns (read succeeded, result)."""
    result = run_code(payload, sandbox=box)
    return READ_SENTINEL in result["stdout"], result


def report_rung(box: Sandbox, payload: str) -> bool:
    """Print one rung's outcome. Returns whether the metadata read landed."""
    reached, result = row_41_run(box, payload)
    verdict = "READ THE CREDENTIALS" if reached else "denied"
    print(f"\n=== rung: {box.name} ===")
    print(f"metadata read   : {verdict}")
    print(f"tool ok         : {result['ok']}")
    print(f"deny log        : {result['denied_egress']}")
    first = result["stdout"].strip().splitlines()[:2]
    print(f"stdout          : {first}")
    return reached


def egress_table(stub: StubNetwork) -> list[str]:
    """Print the policy's answers for four requests. Returns any failures."""
    policy = EgressPolicy(
        frozenset({PUBLIC_HOST, REBIND_HOST}),
        resolver=stub.resolver(),
    )
    cases = [
        (METADATA_HOST, 443, Decision.DENY, "resolves into a blocked range"),
        (PUBLIC_HOST, 443, Decision.ALLOW, "allowlisted, public, on 443"),
        (PUBLIC_HOST, 80, Decision.DENY, "not 443"),
        (REBIND_HOST, 443, Decision.DENY, "one private answer of two"),
    ]
    print("\n=== egress policy, four requests ===")
    failures: list[str] = []
    for host, port, expected, why in cases:
        got = policy.decide(host, port)
        mark = "ok" if got is expected else "UNEXPECTED"
        print(f"{host}:{port:<4} -> {got.value:<5} ({why}) [{mark}]")
        if got is not expected:
            failures.append(f"{host}:{port} decided {got.value}, want {expected.value}")
    return failures


def reset_destroys_the_session(net: NetworkConfig) -> list[str]:
    """Write a file, reset, look again. Returns any failures."""
    box = SubprocessSandbox(net)
    probe = "import os; print('note.txt', os.path.exists('note.txt'))"
    try:
        box.run("open('note.txt', 'w').write('session one')", timeout_s=10)
        before = box.run(probe, timeout_s=10).stdout.strip()
        box.reset()
        after = box.run(probe, timeout_s=10).stdout.strip()
    finally:
        box.close()
    print("\n=== reset() destroys session state ===")
    print(f"before reset    : {before}")
    print(f"after reset     : {after}")
    failures: list[str] = []
    if "True" not in before:
        failures.append("the file was not there before reset()")
    if "False" not in after:
        failures.append("the file survived reset()")
    return failures


def secrets_story(net: NetworkConfig) -> list[str]:
    """Mint a scoped token, and look for secrets inside. Returns failures."""
    broker = SecretsBroker(LONG_LIVED_CREDENTIAL, ttl_s=60.0)
    token = broker.mint(
        SANDBOX_PRINCIPAL, audience="orders-api", scope="sandbox.exec"
    )
    failures: list[str] = []
    print("\n=== secrets: a broker outside, a token inside ===")
    print(f"token           : {token.value[:12]}... (32 hex chars)")
    print(f"audience/scope  : {token.audience} / {token.scope}")
    print(f"holds credential: {LONG_LIVED_CREDENTIAL in token.value}")
    if LONG_LIVED_CREDENTIAL in token.value:
        failures.append("the minted token contains the credential")

    money = Principal.of(None, "sandbox.exec", agent_id="agent:northstar")
    try:
        broker.mint(money, audience="refunds-api", scope="refunds.write")
        failures.append("the broker minted a scope the principal lacks")
        print("refunds.write   : MINTED (this is the bug)")
    except ScopeNotHeld as exc:
        print(f"refunds.write   : refused ({exc})")

    box = SubprocessSandbox(net)
    try:
        listing = box.run(
            "import os; print(' '.join(sorted(os.environ)))", timeout_s=10
        )
    finally:
        box.close()
    names = listing.stdout.split()
    leaked = [name for name in names if looks_like_secret(name)]
    print(f"env in sandbox  : {' '.join(names) or '(empty)'}")
    print(f"secret-like     : {leaked or 'none'}")
    if leaked:
        failures.append(f"secret-named variables visible inside: {leaked}")
    return failures


def allowlist_story(stub: StubNetwork) -> list[str]:
    """Name one host in the policy and fetch it. Returns any failures."""
    box = SubprocessSandbox(stub.network(allow_hosts=frozenset({PUBLIC_HOST})))
    code = (
        "import urllib.request as u\n"
        f'with u.urlopen("{stub.public_url}", timeout=2) as r:\n'
        "    print(r.read().decode().splitlines()[0])\n"
    )
    try:
        result = box.run(code, timeout_s=10)
    finally:
        box.close()
    print("\n=== the allowlist, when the policy names the host ===")
    print(f"host            : {PUBLIC_HOST}")
    print(f"ok              : {result.ok}")
    print(f"stdout          : {result.stdout.strip()!r}")
    print(f"deny log        : {result.denied_egress}")
    if not result.ok:
        return [f"the allowlisted host was refused: {result.stderr[:200]}"]
    return []


def main() -> int:
    """Run every rung, then the other three control surfaces."""
    failures: list[str] = []
    with StubNetwork() as stub:
        payload = ROW_41_PAYLOAD.format(url=stub.metadata_url)
        print("Row 41, replayed through every rung available here.")
        print(f"target          : {stub.metadata_url}")
        print(f"really running  : 127.0.0.1:{stub.port} (loopback stub)")

        boxes = build_ladder(stub.network(), include_control=True)
        try:
            for box in boxes:
                reached = report_rung(box, payload)
                if box.name == CONTROL and not reached:
                    failures.append(
                        "the negative control did not reach the metadata "
                        "stub, so a deny elsewhere proves nothing"
                    )
                if box.name != CONTROL and reached:
                    failures.append(f"{box.name} reached the metadata stub")
        finally:
            close_all(boxes)

        failures += egress_table(stub)
        failures += allowlist_story(stub)
        failures += reset_destroys_the_session(stub.network())
        failures += secrets_story(stub.network())

    print("\n--- what this proves ---")
    print("The same payload, the same reachable target, four control")
    print("surfaces, and one policy object shared by every rung. The only")
    print("implementation that reads the credentials is the one with no")
    print("boundary, which is how you know the others denied rather than")
    print("simply failing to connect.")
    print(f"scrubbed env keys : {sorted(scrub_env({'PATH': '/usr/bin'}))}")

    if failures:
        print("\nFAILED:")
        for line in failures:
            print(f"  - {line}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
