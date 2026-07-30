"""Microsoft Azure: Foundry Agent Service, four methods.

Nothing here imports an Azure SDK. The pure methods work offline.

Azure is strongest on identity and it is where teams most often get caught,
so :meth:`FoundryAgents.principal_for` carries the detail that bites:
publishing creates a stable endpoint *and a distinct identity and RBAC
boundary*, and the permissions used during project development do **not**
transfer to the published agent's identity. Production tool calls fail
until roles are reassigned. That is the identity boundary working, and
:meth:`FoundryAgents.published_identity_gap` makes it a value a test can
assert on rather than a paragraph in a runbook.

As of July 2026: Foundry agents core **[GA]**, hosted agents
**[PREVIEW]**, classic Foundry Agent Service **[DEPRECATED]** and retiring
31 March 2027.
"""

from __future__ import annotations

from northstar_policy import Principal
from northstar_runtime import Checkpointer

from adapters.base import CloudUnavailable, ExitCost

__all__ = ["FoundryAgents"]

#: A resumable envelope around a process that idles down, not thirty days
#: of running compute — and not a journal either.
MAX_LOGICAL_SESSION_SECONDS = 30 * 24 * 3600


class FoundryAgents:
    """Foundry Agent Service as a four-method adapter.

    Args:
        region: Where the project lives.
        project: The isolation unit. Note that it is the *project* rather
            than the agent: agents in one project can share storage,
            search, and context resources, which surprises teams whose
            tenancy model assumed per-agent isolation.
        hosted: Use hosted agents, which are preview, rather than prompt
            agents. Real feature depth, real preview risk.
    """

    name = "azure"

    def __init__(
        self,
        region: str,
        project: str = "northstar",
        hosted: bool = False,
    ) -> None:
        self.region = region
        self.project = project
        self.hosted = hosted

    def session_store(self) -> Checkpointer:
        """Conversations and per-session filesystem state.

        Raises:
            CloudUnavailable: Always, offline.
        """
        raise CloudUnavailable(
            "Foundry sessions need the Azure SDK and a project. "
            "Run: pip install azure-ai-projects, then set "
            "AZURE_SUBSCRIPTION_ID. The scorecard runs against "
            "adapters.mock without either."
        )

    def tool_endpoint(self) -> str:
        """The curated toolbox, over MCP.

        Hosted agents consume curated tools through a toolbox endpoint
        rather than having arbitrary tools injected into the container,
        which limits Chapter 18's tool-poisoning surface by construction.
        """
        return (
            f"https://{self.project}.{self.region}.api.azureml.ms/"
            f"agents/toolbox/mcp"
        )

    def principal_for(self, inbound: dict) -> Principal:
        """Map Entra inbound authorization onto the three identities.

        Four things stay separate here and conflating any two of them is
        the usual Azure mistake: inbound authorization (who may invoke the
        application), outbound agent identity (what the agent may reach),
        delegated user authority (what it may do for a specific user), and
        the distribution channel.
        """
        agent = inbound.get("agent_identity") or {}
        user = inbound.get("user") or {}
        return Principal(
            user_id=user.get("oid") or user.get("upn"),
            agent_id=str(agent.get("app_id", "northstar-support-agent")),
            operator_id=str(inbound.get("tenant_id", "northstar-platform")),
            # The published agent's *own* roles, not the developer's.
            scopes=frozenset(agent.get("roles") or ()),
        )

    def exporter(self) -> str:
        """Application Insights, over OTLP."""
        return f"otlp://{self.region}.applicationinsights.azure.com:4317"

    # -------------------------------------------------- scorecard inputs

    def published_identity_gap(self, inbound: dict) -> list[str]:
        """Scopes a developer held that the published agent does not.

        Test the permission migration deliberately. The failure otherwise
        appears as production tool calls failing for a system that worked
        all through testing.
        """
        developer = set((inbound.get("developer") or {}).get("roles") or ())
        published = set(self.principal_for(inbound).scopes)
        return sorted(developer - published)

    def cold_start_ms(self) -> int | None:
        """Unmeasured, so ``None``."""
        return None

    def preview_dependencies(self) -> int:
        """One if the design uses hosted agents, which are preview."""
        return 1 if self.hosted else 0

    def exit_cost(self) -> ExitCost:
        """Entra, the toolbox, and Microsoft 365 distribution are gravity."""
        return ExitCost(
            self.name,
            travels=("agent code", "tool contracts", "graders", "spans"),
            rebuilt=(
                "session filesystem state and conversation store",
                "curated toolbox configuration",
                "Entra Agent ID lifecycle, ownership, conditional access",
                "Application Insights dashboards and alerts",
            ),
            preview_dependencies=self.preview_dependencies(),
        )
