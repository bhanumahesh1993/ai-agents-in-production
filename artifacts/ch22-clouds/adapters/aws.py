"""AWS: Bedrock AgentCore, behind the same four methods.

Nothing here imports ``boto3``. :meth:`AgentCore.session_store` raises with
the install command named, and the three pure methods work offline, which
is where the interesting differences live anyway: how inbound
authorization maps onto the three identities, and where the enforcement
point sits.

The policy story is the cleanest of the three, and it is a chain rather
than a component::

    User (JWT or IAM)
      -> AgentCore Runtime
      -> Gateway
      -> Cedar policy + guardrails
      -> credential broker
      -> MCP / API / function target

Every arrow is a place Chapter 19's least agency and Chapter 20's action
classes get enforced by infrastructure rather than by prompt.

As of July 2026: AgentCore **[GA]** on a June 2026 feature baseline, with
the release that restricts Runtime so invocations must originate from
Gateway. Re-verify before you rely on any of it; ``VERSIONS.md`` records
what this edition was checked against.
"""

from __future__ import annotations

from northstar_policy import Principal
from northstar_runtime import Checkpointer

from adapters.base import CloudUnavailable, ExitCost

__all__ = ["AgentCore"]

#: The longest a session's microVM lives. A generous session, and not a
#: multi-day workflow: business checkpoints and side-effect evidence
#: belong in stores outside the ephemeral environment.
MAX_SESSION_SECONDS = 8 * 3600


class AgentCore:
    """Amazon Bedrock AgentCore as a four-method adapter.

    Args:
        region: The region the endpoint lives in. There is no default:
            residency is a decision, not an inherited one.
        gateway_id: The Gateway that fronts the tools and holds the policy.
    """

    name = "aws"

    def __init__(self, region: str, gateway_id: str = "northstar-gateway") -> None:
        self.region = region
        self.gateway_id = gateway_id

    def session_store(self) -> Checkpointer:
        """AgentCore Memory, as a checkpointer.

        Raises:
            CloudUnavailable: Always, offline. The session store is the
                one method of the four that genuinely needs the account,
                and it is also the least portable thing the platform sells,
                which is not a coincidence.
        """
        raise CloudUnavailable(
            "AgentCore Memory needs the AWS SDK and an account. "
            'Run: pip install boto3, then set AWS_REGION. '
            "The scorecard runs against adapters.mock without either."
        )

    def tool_endpoint(self) -> str:
        """Gateway. It is also the enforcement point, which matters more."""
        return (
            f"https://{self.gateway_id}.gateway.bedrock-agentcore."
            f"{self.region}.amazonaws.com/mcp"
        )

    def principal_for(self, inbound: dict) -> Principal:
        """Map IAM or JWT inbound authorization onto the three identities.

        Inbound authorization accepts IAM or JWT/OIDC. Either way the
        *agent* is a workload identity distinct from the user, which is the
        property the mapping has to preserve: collapsing them here would
        undo Chapter 19 one layer below where it was built.
        """
        claims = inbound.get("claims") or {}
        actor = claims.get("act") or {}
        return Principal(
            user_id=claims.get("sub") or inbound.get("iam_user"),
            agent_id=str(
                actor.get("sub")
                or inbound.get("workload_identity")
                or "northstar-support-agent"
            ),
            operator_id=str(inbound.get("account_alias", "northstar-platform")),
            scopes=frozenset(str(claims.get("scope", "")).split()),
        )

    def exporter(self) -> str:
        """OTLP into the account's monitoring surfaces.

        If you already run a cross-cloud OpenTelemetry platform, expect a
        translation layer rather than a drop-in.
        """
        return f"otlp://cloudwatch.{self.region}.amazonaws.com:4317"

    # -------------------------------------------------- scorecard inputs

    def cold_start_ms(self) -> int | None:
        """Unmeasured, so ``None``. Never a vendor figure."""
        return None

    def preview_dependencies(self) -> int:
        """Capabilities this design leans on that are still preview."""
        return 0

    def exit_cost(self) -> ExitCost:
        """The deepest controls create the strongest gravity."""
        return ExitCost(
            self.name,
            travels=("agent code", "tool contracts", "graders", "spans"),
            rebuilt=(
                "Gateway configuration",
                "Identity integration and the credential broker",
                "Cedar policy authored against Gateway's model",
                "operations built on the native monitoring shape",
            ),
            preview_dependencies=self.preview_dependencies(),
        )
