"""Google Cloud: the Gemini Enterprise Agent Platform, four methods.

Nothing here imports a Google SDK. The pure methods work offline.

Two things about this platform are worth carrying away regardless of
whether you deploy on it. Memory Bank is the most thoroughly specified
managed-memory product currently documented, and Agent Identity is
Chapter 19's identity triangle implemented as a product: a SPIFFE-based
cryptographic identity that distinguishes the agent's own authority from
delegated user authority and can keep raw delegated credentials out of
agent code entirely.

The caveat is status. Gateway and observability are GA while the Agent
Identity API and Agent Registry are **[PREVIEW]**, so
:meth:`AgentPlatform.preview_dependencies` returns a non-zero count when
the design leans on them. Design so the GA baseline works and the preview
path is an upgrade rather than a load-bearing assumption.

As of July 2026, following the April 2026 reorganization. The Vertex-era
names still appear in SDKs and documentation in the wild; pin the
documentation date in any decision record.
"""

from __future__ import annotations

from northstar_policy import Principal
from northstar_runtime import Checkpointer

from adapters.base import CloudUnavailable, ExitCost

__all__ = ["AgentPlatform"]

#: The longest managed execution window of the three. Still bounded, and
#: still not a journal of which side effects committed.
MAX_OPERATION_SECONDS = 7 * 24 * 3600


class AgentPlatform:
    """The Gemini Enterprise Agent Platform as a four-method adapter.

    Args:
        region: Where Runtime, Sessions, and Memory Bank live.
        project: The project the resources belong to.
        use_agent_identity: Whether the design depends on the Agent
            Identity API, which is preview. Turning it on raises the
            preview-dependency count, which is the number that predicts
            unplanned work.
    """

    name = "gcp"

    def __init__(
        self,
        region: str,
        project: str = "northstar",
        use_agent_identity: bool = False,
    ) -> None:
        self.region = region
        self.project = project
        self.use_agent_identity = use_agent_identity

    def session_store(self) -> Checkpointer:
        """Sessions plus Memory Bank, as a checkpointer.

        Raises:
            CloudUnavailable: Always, offline.
        """
        raise CloudUnavailable(
            "Agent Runtime Sessions need the Google Cloud SDK and a "
            "project. Run: pip install google-cloud-aiplatform, then set "
            "GOOGLE_CLOUD_PROJECT. The scorecard runs against "
            "adapters.mock without either."
        )

    def tool_endpoint(self) -> str:
        """Agent Gateway: one boundary for user, tool, and agent traffic."""
        return (
            f"https://{self.region}-agentgateway.googleapis.com/v1/"
            f"projects/{self.project}/mcp"
        )

    def principal_for(self, inbound: dict) -> Principal:
        """Map a SPIFFE-attested agent plus delegated user onto three ids.

        The agent's own authority and the delegated user authority arrive
        as separate facts here, which is the whole point of the product and
        the reason this mapping is three lines rather than a guess.
        """
        spiffe = str(inbound.get("spiffe_id", ""))
        return Principal(
            user_id=(inbound.get("delegated") or {}).get("sub"),
            agent_id=spiffe.rsplit("/", 1)[-1] or "northstar-support-agent",
            operator_id=str(inbound.get("project", self.project)),
            scopes=frozenset(
                (inbound.get("delegated") or {}).get("scopes") or ()
            ),
        )

    def exporter(self) -> str:
        """Agent Observability consumes OpenTelemetry data directly."""
        return f"otlp://telemetry.{self.region}.googleapis.com:4317"

    # -------------------------------------------------- scorecard inputs

    def cold_start_ms(self) -> int | None:
        """Unmeasured, so ``None``.

        The platform claims sub-second cold starts. That is a vendor claim
        about reference conditions, and repeating it as your own number is
        the pitfall this field exists to make impossible.
        """
        return None

    def preview_dependencies(self) -> int:
        """One if the design leans on the preview Agent Identity API."""
        return 1 if self.use_agent_identity else 0

    def exit_cost(self) -> ExitCost:
        """The native kit is the smoothest path, and therefore gravity."""
        return ExitCost(
            self.name,
            travels=("agent code", "tool contracts", "graders", "spans"),
            rebuilt=(
                "Sessions and Memory Bank contents and shape",
                "Agent Gateway configuration",
                "Agent Identity and its X.509 binding",
                "evaluation configured in the managed evaluator",
            ),
            preview_dependencies=self.preview_dependencies(),
        )
