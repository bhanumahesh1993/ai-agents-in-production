"""``run_code``: the tool, and the identity it does not have.

Chapter 11 shipped this tool against a null sandbox that executed in the
calling process and was labelled a placeholder. This is the replacement.
The tool never talks to a sandbox directly; it talks to the interface,
which is what makes the isolation rung a configuration change rather than
a rewrite.

``idempotent=False`` is the honest declaration, and it has a consequence:
because a retry re-executes arbitrary code, this tool is never given a
scope that moves money. The idempotency-key discipline from Chapter 1
works because ``issue_refund`` can recognise a repeated intent. Model-
written Python cannot, so the mitigation is not a key, it is keeping the
two capabilities in different processes with different identities.
"""

from __future__ import annotations

import functools

from northstar_contracts import ToolSpec
from northstar_policy import Principal
from northstar_runtime import ToolRegistry
from sandbox import Sandbox

__all__ = ["RUN_CODE", "SANDBOX_PRINCIPAL", "registry_for", "run_code"]

RUN_CODE = ToolSpec(
    name="run_code",
    description="Run Python in an isolated sandbox. No network by default.",
    input_schema={"type": "object", "required": ["code"],
                  "properties": {"code": {"type": "string"}}},
    output_schema={"type": "object"},
    writes=True,        # mutates the sandbox; never the Northstar world
    idempotent=False,   # re-running re-runs whatever the code did
    max_result_tokens=800,
)

#: The identity the sandbox runs under. One scope, and it is not a verb
#: that moves money. Chapter 19 develops the model this depends on.
SANDBOX_PRINCIPAL = Principal.of(
    None,
    "sandbox.exec",
    agent_id="agent:northstar-support",
)


def run_code(code: str, *, sandbox: Sandbox) -> dict:
    """Run model-written Python and return the bounded result."""
    # Principal agent:northstar-support, scopes={"sandbox.exec"} only.
    # refunds.write is not in this principal's scope set, so no code
    # path from inside this sandbox can reach issue_refund.
    res = sandbox.run(code, timeout_s=20)
    return {"ok": res.ok, "stdout": res.stdout[:4000],
            "denied_egress": res.denied_egress}


def registry_for(sandbox: Sandbox) -> ToolRegistry:
    """A registry holding just this tool, bound to one sandbox.

    The binding happens here, outside anything the model can influence.
    Which sandbox the code runs in is a deployment decision, and the
    model's arguments do not reach it.
    """
    registry = ToolRegistry()
    registry.register(RUN_CODE, functools.partial(run_code, sandbox=sandbox))
    return registry
