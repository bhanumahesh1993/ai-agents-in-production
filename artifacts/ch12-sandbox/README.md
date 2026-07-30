# Chapter 12 — a code-execution tool behind a swappable sandbox

**What this artifact proves:** the containment of a code-execution tool comes
from a policy object and a boundary you can point at and test, not from the
model's cooperation — the chapter's row-41 payload reads a *live, reachable*
metadata service at the one rung with no boundary, and is denied at every rung
above it by the same `EgressPolicy` object, whose deny list rides back to the
caller in the tool result.

## Run it

```bash
python artifacts/ch12-sandbox/demo.py
# and the suite
pytest -q artifacts/ch12-sandbox
```

The demo replays the payload from the chapter's opening through every rung
available on this machine and prints, for each one, whether the metadata read
succeeded, what the tool returned, and the resulting deny log. Then it walks the
other three control surfaces: the policy's answer to four requests, an
allowlisted host succeeding when the policy names it, a file written before
`reset()` and missing after it, and a broker that mints a scoped token while the
credential stays outside.

It exits non-zero if any isolated rung fails to deny the read, **or if the
in-process negative control fails to make it**. That second condition is the
part worth copying. A metadata-endpoint test that passes on a laptop with no
route to link-local proves nothing and reports a control you have not built, so
the target here is a real HTTP server on `127.0.0.1`, resolvable inside the
sandbox as `metadata.test`, and one implementation has to be able to reach it or
every deny in the suite is free.

## Files

| File | What it is |
|---|---|
| `sandbox.py` | The interface the chapter prints: `SandboxResult` and the three-member `Sandbox` protocol, plus the exit codes that keep a timeout distinguishable from a crash. |
| `egress.py` | `BLOCKED`, `EgressPolicy.decide`, and the injectable resolver. Every resolved address is checked, not the first; an unresolvable name is a deny; the default construction allows nothing. |
| `netshim.py` | Where the egress hook goes: in front of `urlopen`, inside the environment that runs the code. Also the child prelude that installs the hook and the resource limits before reading the code it was asked to run. |
| `broker.py` | The secrets broker — long-lived credential outside, short-lived audience-and-scope-bound token in — plus `SECRET_NAME_PATTERNS` and the environment allowlist the subprocess rung builds its child env from. |
| `inprocess.py` | The negative control. `exec()` in the agent's own process. Contains nothing, and is labelled as such in the first line of the module. |
| `subproc.py` | The first real rung: separate process group, scrubbed environment, non-root check, wall-clock timeout that kills the group, kernel-enforced file-size quota, CPU backstop, output cap. |
| `container.py` | `docker run` with `--network none`, `--read-only`, `--user 65534`, `--cap-drop ALL`, pid and memory limits. Opt-in and skips itself cleanly when Docker is absent. |
| `microvm.py` | A stub microVM adapter that delegates to the subprocess rung and says on itself that there is no hypervisor. It exists so a real Firecracker adapter has assertions to satisfy. |
| `ladder.py` | Which rungs exist here, weakest first, so the suite parameterises over the answer instead of a list that goes stale. |
| `stub_network.py` | The loopback stand-in: one HTTP server serving a stub metadata document under `metadata.test` and a CSV under `files.northstar.test`, with the resolver table and routes they imply. |
| `tool.py` | `RUN_CODE` and `run_code(code, *, sandbox)`. The identity comment is load-bearing: the principal holds `sandbox.exec` and never `refunds.write`. |
| `demo.py` | Runs all of it and exits non-zero if the inversion does not hold. |
| `test_ch12.py` | Filesystem, time and resources, secrets, and the tool, asserted on behaviour. |
| `tests/test_egress.py` | The egress surface, including the chapter's `test_metadata_endpoint_is_denied`, run against every available implementation. |
| `conftest.py` | Makes `import sandbox` mean *this* chapter's sandbox when the whole `artifacts/` tree runs under one pytest, and holds the fixtures both test files share. |

## Which containment claims are real, and which are mocked

The chapter's argument is that egress and credentials are configuration on every
rung rather than properties of any of them, so this artifact implements those
two properly and is explicit about the rest.

**Real, and tested:**

- The egress decision. `EgressPolicy` resolves the name, checks *every* answer
  against `BLOCKED`, denies anything that is not port 443, and denies any host
  the allowlist does not name. The metadata target is running and reachable; the
  deny comes from the policy.
- Process isolation at the subprocess rung: a separate interpreter, its own
  process group, and a wall-clock kill that takes the group with it.
- The environment. The child's environment is built from an allowlist, so no
  variable whose name matches `SECRET_NAME_PATTERNS` reaches the code.
- The secrets broker. The credential never leaves the parent, the token is
  derived, and minting a scope the principal does not hold raises.
- Session destruction. `reset()` deletes the scratch directory, and the test
  asserts a file written before the call is absent after it.
- The scratch quota, via `RLIMIT_FSIZE` set as a hard limit in the child before
  it reads the code — so it is the kernel refusing the write, not a check the
  code could skip.
- The output cap, twice: once in the sandbox and once in `run_code`.

**Mocked, or absent, and named here rather than implied:**

- **The proxy.** In production the hook is a forward proxy that the sandbox is
  the only route to, with the network layer denying everything else. This
  artifact puts the same decision at the same point in the request path, in
  front of `urlopen`, inside the environment running the code. That is enough to
  prove the policy, and it is *not* containment: code that opens a raw socket,
  or that re-imports `netshim` off `sys.modules`, is not stopped at the
  subprocess rung. Two layers, each doing the part it is good at — this repo can
  only stand up one of them offline.
- **Syscall filtering.** seccomp-BPF is Linux-only and this suite has to run on
  a laptop, so the subprocess rung has no syscall filter. That is the difference
  between "a separate process" and "the first real rung" as the chapter defines
  it.
- **The microVM.** `microvm.py` boots nothing. It has no guest kernel and no
  hardware boundary, and `provides_hardware_boundary = False` says so in code.
- **The container.** `container.py` is real code with the right flags, but it is
  off unless `NORTHSTAR_CH12_DOCKER=1` is set, the `docker` CLI works, *and* the
  image is already present locally — an offline suite must not pull an image.
  With `--network none` it has no route to the loopback stub, so it proves the
  deny and skips the allowlist-succeeds case; `can_reach_loopback` is the
  attribute that records this.
- **TLS.** The stub speaks plain HTTP on loopback. The policy still evaluates
  the request the code actually made, scheme and port included; only the socket
  underneath is different.

## Read `egress.py` first, then `tests/test_egress.py`

`decide()` is nine lines and two of them are the security property:

```python
for addr in self.resolver(host):     # every A and AAAA answer
    if is_blocked(addr):
        return Decision.DENY
```

A name that resolves to one permitted address and one private one is a rebinding
attack, and a first-answer check waves it through. `rebind.test` in the stub
network exists to make that concrete: it is *in* the allowlist in
`test_every_resolved_address_is_checked`, and it is still denied.

The other half is in the fixture rather than the assertion. `stub_metadata` is a
URL for a server that is up. Take the server away and the same test still passes
for the wrong reason, which is the failure mode the chapter warns about and the
reason this artifact ships `test_the_stub_is_genuinely_reachable` beside it.
