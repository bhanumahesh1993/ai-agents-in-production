"""The other three control surfaces, and the inversion that anchors them.

Filesystem, time and resources, and secrets, asserted on behaviour: what
is on disk after ``reset()``, what exit status an infinite loop produces,
what a 4 MB write does against a 256 KB quota, and which environment
variable names survive into the child. The egress surface has its own
file, one directory down.

The first test is the one that gives the rest their meaning. If nothing
could reach the stub, every deny elsewhere would be free.
"""

from __future__ import annotations

import os

import pytest
from broker import (
    SECRET_NAME_PATTERNS,
    ScopedToken,
    ScopeNotHeld,
    SecretsBroker,
    looks_like_secret,
    scrub_env,
)
from ladder import CONTROL, build_ladder, close_all
from netshim import NetworkConfig
from northstar_contracts import ToolCall
from northstar_policy import Principal
from sandbox import TIMEOUT_EXIT_CODE, Sandbox, SandboxResult
from stub_network import METADATA_HOST, StubNetwork
from tool import RUN_CODE, SANDBOX_PRINCIPAL, registry_for, run_code

CREDENTIAL = "EXAMPLE-northstar-orders-service-credential"


def payload_for(url: str) -> str:
    """Row 41, reduced to the step the sandbox has to stop."""
    return (
        "import urllib.request as u\n"
        f'with u.urlopen("{url}", timeout=2) as r:\n'
        '    print("CREDENTIALS-READ:", r.read().decode()[:32])\n'
    )


# --------------------------------------------------------------- inversion


def test_only_the_negative_control_reads_the_metadata_stub(
    stub_service: StubNetwork,
    no_egress_net: NetworkConfig,
) -> None:
    """The control succeeds; every isolated rung denies. Both halves."""
    code = payload_for(stub_service.metadata_url)
    boxes = build_ladder(no_egress_net, include_control=True)
    try:
        results = {box.name: box.run(code, timeout_s=10) for box in boxes}
    finally:
        close_all(boxes)

    control = results.pop(CONTROL)
    assert control.ok, control.stderr
    assert "CREDENTIALS-READ:" in control.stdout
    assert control.denied_egress == []

    assert results, "no isolated rung was available to compare against"
    for name, res in results.items():
        assert not res.ok, f"{name} ran the payload to completion"
        assert "CREDENTIALS-READ:" not in res.stdout, name
        assert res.denied_egress == [METADATA_HOST], name


# -------------------------------------------------------------- filesystem


def test_reset_destroys_the_session_filesystem(sandbox: Sandbox) -> None:
    """A file written in one session is absent in the next."""
    probe = "import os; print(os.path.exists('note.txt'))"
    sandbox.run("open('note.txt', 'w').write('session one')", timeout_s=10)
    before = sandbox.run(probe, timeout_s=10)
    assert before.stdout.strip() == "True", before.stderr
    sandbox.reset()
    after = sandbox.run(probe, timeout_s=10)
    assert after.stdout.strip() == "False", after.stderr


def test_the_scratch_quota_stops_a_large_write(sandbox: Sandbox) -> None:
    """A small write lands; one past the quota does not."""
    small = sandbox.run(
        "open('small.bin', 'w').write('x' * 1024)", timeout_s=10
    )
    assert small.ok, small.stderr
    big = sandbox.run(
        "open('big.bin', 'w').write('x' * 4_000_000)", timeout_s=15
    )
    assert not big.ok
    assert big.exit_code != 0


# --------------------------------------------------------- time and limits


def test_a_timeout_is_reported_as_a_timeout_not_a_crash(
    sandbox: Sandbox,
) -> None:
    """An infinite loop is killed, and the result says so."""
    res = sandbox.run("while True:\n    pass\n", timeout_s=1)
    assert not res.ok
    assert res.timed_out
    assert res.exit_code == TIMEOUT_EXIT_CODE
    assert "Traceback" not in res.stderr
    assert res.duration_ms < 20_000


def test_the_process_does_not_run_as_root(sandbox: Sandbox) -> None:
    """Non-root execution is not optional, so it is asserted."""
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        pytest.skip("the test runner is root; the sandbox refuses to run")
    res = sandbox.run("import os; print(os.getuid())", timeout_s=10)
    assert res.ok, res.stderr
    assert int(res.stdout.strip()) != 0


def test_output_is_capped_before_it_reaches_the_model(
    sandbox: Sandbox,
) -> None:
    """A tool that can return a gigabyte is a bill, not a feature."""
    result = run_code("print('x' * 10_000)", sandbox=sandbox)
    assert result["ok"]
    assert len(result["stdout"]) == 4000


# -------------------------------------------------------------- the secrets


def test_no_secret_named_environment_variable_is_visible(
    sandbox: Sandbox,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The environment the code can read holds nothing worth stealing."""
    monkeypatch.setenv("NORTHSTAR_API_KEY", "EXAMPLE-would-be-stolen")
    monkeypatch.setenv("ORDERS_DB_PASSWORD", "EXAMPLE-would-be-stolen")
    monkeypatch.setenv("AWS_SESSION_TOKEN", "EXAMPLE-would-be-stolen")
    res = sandbox.run(
        "import os; print(' '.join(sorted(os.environ)))", timeout_s=10
    )
    assert res.ok, res.stderr
    names = res.stdout.split()
    assert [name for name in names if looks_like_secret(name)] == []
    assert "NORTHSTAR_API_KEY" not in names
    res = sandbox.run(
        "import os; print(len(os.environ.get('ORDERS_DB_PASSWORD', '')))",
        timeout_s=10,
    )
    assert res.stdout.strip() == "0"


def test_scrub_env_keeps_an_allowlist_and_refuses_to_add_secrets() -> None:
    """The scrub is an allowlist, and it will not be talked out of it."""
    dirty = {"PATH": "/usr/bin", "STRIPE_API_KEY": "x", "HOSTNAME": "nr-1"}
    clean = scrub_env(dirty)
    assert set(clean) == {"PATH"}
    with pytest.raises(ValueError, match="refusing to pass"):
        scrub_env(dirty, extra={"NORTHSTAR_TOKEN": "x"})


def test_the_broker_never_hands_over_the_credential() -> None:
    """What crosses the boundary is derived, scoped, and short-lived."""
    broker = SecretsBroker(CREDENTIAL, ttl_s=60.0)
    token = broker.mint(
        SANDBOX_PRINCIPAL, audience="orders-api", scope="sandbox.exec"
    )
    assert isinstance(token, ScopedToken)
    assert CREDENTIAL not in token.value
    assert len(token.value) == 32
    assert token.is_valid_for("orders-api", "sandbox.exec")


def test_a_token_is_bound_to_one_audience_one_scope_and_a_deadline() -> None:
    """It cannot be replayed sideways, and it does not last."""
    now = [1000.0]
    broker = SecretsBroker(CREDENTIAL, ttl_s=30.0, clock=lambda: now[0])
    token = broker.mint(
        SANDBOX_PRINCIPAL, audience="orders-api", scope="sandbox.exec"
    )
    assert not token.is_valid_for("refunds-api", "sandbox.exec", now=now[0])
    assert not token.is_valid_for("orders-api", "refunds.write", now=now[0])
    assert token.is_valid_for("orders-api", "sandbox.exec", now=now[0] + 29)
    assert not token.is_valid_for("orders-api", "sandbox.exec", now=now[0] + 31)


def test_the_broker_refuses_a_scope_the_principal_does_not_hold() -> None:
    """The sandbox principal cannot mint its way to a refund."""
    broker = SecretsBroker(CREDENTIAL)
    with pytest.raises(ScopeNotHeld):
        broker.mint(
            SANDBOX_PRINCIPAL, audience="refunds-api", scope="refunds.write"
        )
    assert broker.audit[-1]["event"] == "mint.denied"


def test_the_run_code_principal_cannot_move_money() -> None:
    """One scope, and it is not a verb that moves money."""
    assert SANDBOX_PRINCIPAL.has("sandbox.exec")
    assert not SANDBOX_PRINCIPAL.has("refunds.write")
    assert SANDBOX_PRINCIPAL.scopes == frozenset({"sandbox.exec"})
    refunder = Principal.of("CUST-8841", "refunds.write")
    assert not refunder.has("sandbox.exec")


def test_every_secret_pattern_matches_something_a_deployment_would_set() -> None:
    """The pattern list is not decorative."""
    for pattern in SECRET_NAME_PATTERNS:
        assert looks_like_secret(f"NORTHSTAR_{pattern}_V2")


# ------------------------------------------------------------------ the tool


def test_the_tool_result_carries_the_deny_list(
    sandbox: Sandbox,
    stub_metadata: str,
) -> None:
    """The deny is evidence, so it rides back in the tool result."""
    registry = registry_for(sandbox)
    call = ToolCall("c1", "run_code", {"code": payload_for(stub_metadata)})
    result = registry.dispatch(call, run_id="run_ch12", step=1)
    assert result.ok  # the dispatch worked; the *code* did not
    assert result.content["ok"] is False
    assert result.content["denied_egress"] == [METADATA_HOST]
    assert "CREDENTIALS-READ:" not in result.content["stdout"]


def test_the_spec_declares_what_the_runtime_needs_to_know() -> None:
    """``writes`` and ``idempotent`` are read by policy and by retries."""
    assert RUN_CODE.writes is True
    assert RUN_CODE.idempotent is False
    assert RUN_CODE.max_result_tokens == 800


def test_a_result_knows_a_timeout_from_a_failure() -> None:
    """``timed_out`` is derived from the exit code, not from a string."""
    timed = SandboxResult(False, "", "", TIMEOUT_EXIT_CODE, 1000, [])
    failed = SandboxResult(False, "", "boom", 1, 4, [])
    assert timed.timed_out
    assert not failed.timed_out
