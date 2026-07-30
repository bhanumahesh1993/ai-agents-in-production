# artifacts/ch22-clouds/iac/azure/main.tf
#
# Opt-in. Nothing in `make demo-ch22` applies this file.
#
# What it creates: one Foundry project, one published agent application
# with its own Entra identity, the curated toolbox endpoint, and the role
# assignment the published identity needs.
#
# The role assignment is the point of this file. Publishing creates a
# stable endpoint *and a distinct identity and RBAC boundary*, and the
# permissions used during project development do not transfer. Production
# tool calls fail until roles are reassigned, and teams read that as a bug
# on their first day. Declaring the assignment here is what makes the
# permission migration a reviewed change rather than an incident.
#
# Note also that the *project* is the isolation unit, not the agent. Agents
# in one project can share storage, search, and context resources. If your
# tenancy model assumed per-agent isolation you need a project per
# boundary, and finding that out during a security review is expensive.
#
# What it costs while it exists: model tokens, tools, knowledge
# connections, and licences; hosted agents additionally bill on container
# compute; published applications can introduce publisher-paid
# infrastructure cost; and enterprise licensing for the control and
# distribution layer is separate from runtime cost.

terraform {
  required_version = ">= 1.9"
}

variable "region" {
  type        = string
  description = "Residency is a decision, not an inherited default."
}

variable "tenant_id" {
  type = string
}

variable "approval_threshold_cents" {
  type    = number
  default = 5000
}

resource "azurerm_ai_foundry_project" "northstar" {
  name     = "northstar"
  location = var.region
}

resource "azurerm_ai_foundry_agent" "support_agent" {
  name       = "northstar-support-agent"
  project_id = azurerm_ai_foundry_project.northstar.id
  hosted     = false
  published  = true
}

resource "azuread_agent_identity" "support_agent" {
  display_name = "northstar-support-agent"
  tenant_id    = var.tenant_id
  owner        = "northstar-platform"
  sponsor      = "northstar-platform"
  expires_on   = "2027-07-30"
}

resource "azurerm_role_assignment" "published_agent_roles" {
  principal_id = azuread_agent_identity.support_agent.object_id
  scope        = azurerm_ai_foundry_project.northstar.id
  role         = "Northstar Refunds Operator"
}

resource "azurerm_ai_foundry_toolbox" "tools" {
  project_id               = azurerm_ai_foundry_project.northstar.id
  protocol                 = "mcp"
  approval_threshold_cents = var.approval_threshold_cents
}

output "tool_endpoint" {
  value = azurerm_ai_foundry_toolbox.tools.mcp_url
}
