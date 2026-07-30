"""The deny, proved against a target that is actually running.

The fixture matters more than the assertion. A test that tries to reach
``169.254.169.254`` from a machine with no route to it passes whether or
not the policy exists, which makes it worse than no test: it reports a
control you do not have. So ``stub_metadata`` is a service on loopback,
listening, resolvable inside the sandbox under a test hostname, and a
pass here means the policy denied.

Every case runs against every available implementation, so adding a
microVM adapter later means proving it satisfies these assertions rather
than trusting that a stronger rung must be safer.
"""

from __future__ import annotations

import pytest
from egress import BLOCKED, EgressPolicy, StaticResolver, is_blocked
from northstar_policy import Decision
from sandbox import Sandbox
from stub_network import (
    METADATA_HOST,
    PUBLIC_HOST,
    REBIND_HOST,
    StubNetwork,
)


def test_metadata_endpoint_is_denied(
    sandbox: Sandbox,
    stub_metadata: str,
) -> None:
    """The chapter's test: the payload's read is refused at every rung."""
    # stub_metadata runs on 127.0.0.1 and is resolvable inside the
    # sandbox, so a pass here means the policy denied, not that the
    # network was simply unavailable.
    code = f"import urllib.request as u\nu.urlopen('{stub_metadata}')"
    res = sandbox.run(code, timeout_s=10)  # read-only probe
    assert not res.ok
    assert res.denied_egress == [METADATA_HOST]


def test_the_stub_is_genuinely_reachable(stub_service: StubNetwork) -> None:
    """Prove the target is up, so the deny above is not an outage."""
    import urllib.request
    from urllib.parse import urlsplit

    address, port = stub_service.routes()[METADATA_HOST]
    path = urlsplit(stub_service.metadata_url).path
    url = f"http://{address}:{port}{path}"
    with urllib.request.urlopen(url, timeout=5) as response:
        body = response.read().decode()
    assert "AccessKeyId" in body


def test_allowlisted_host_succeeds_when_the_policy_names_it(
    allowing_sandbox: Sandbox,
    stub_service: StubNetwork,
) -> None:
    """The allowlist is an allowlist, not a synonym for "off"."""
    if not getattr(allowing_sandbox, "can_reach_loopback", True):
        pytest.skip(f"{allowing_sandbox.name} has no route to the stub")
    code = (
        "import urllib.request as u\n"
        f'with u.urlopen("{stub_service.public_url}", timeout=2) as r:\n'
        "    print(r.read().decode().splitlines()[0])\n"
    )
    res = allowing_sandbox.run(code, timeout_s=10)
    assert res.ok, res.stderr
    assert res.stdout.startswith("order_id,amount_cents")
    assert res.denied_egress == []


def test_every_resolved_address_is_checked(stub_service: StubNetwork) -> None:
    """One private answer among two is a rebind, and it is a deny."""
    policy = EgressPolicy(
        frozenset({REBIND_HOST}), resolver=stub_service.resolver()
    )
    answers = stub_service.resolver()(REBIND_HOST)
    assert len(answers) > 1 and not is_blocked(answers[0])
    assert policy.decide(REBIND_HOST, 443) is Decision.DENY


def test_a_port_other_than_443_is_denied(stub_service: StubNetwork) -> None:
    """Plain HTTP to an allowlisted public host is still a deny."""
    policy = EgressPolicy(
        frozenset({PUBLIC_HOST}), resolver=stub_service.resolver()
    )
    assert policy.decide(PUBLIC_HOST, 443) is Decision.ALLOW
    assert policy.decide(PUBLIC_HOST, 80) is Decision.DENY


def test_the_default_construction_denies_everything(
    stub_service: StubNetwork,
) -> None:
    """``EgressPolicy(allow_hosts=frozenset())`` is not a formality."""
    policy = EgressPolicy(frozenset(), resolver=stub_service.resolver())
    assert policy.decide(PUBLIC_HOST, 443) is Decision.DENY
    assert policy.decide(METADATA_HOST, 443) is Decision.DENY


def test_an_unresolvable_name_is_denied() -> None:
    """A name with no answer is denied: could-not-check is not safe."""
    policy = EgressPolicy(
        frozenset({"nowhere.test"}), resolver=StaticResolver()
    )
    assert policy.decide("nowhere.test", 443) is Decision.DENY


def test_a_literal_link_local_address_is_denied() -> None:
    """The real metadata address, checked without a name to resolve."""
    policy = EgressPolicy(
        frozenset({"169.254.169.254"}), resolver=StaticResolver()
    )
    assert policy.decide("169.254.169.254", 443) is Decision.DENY
    assert any("169.254" in str(net) for net in BLOCKED)
