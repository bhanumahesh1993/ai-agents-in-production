# artifacts/ch22-clouds/iac/gcp/main.tf
#
# Opt-in. Nothing in `make demo-ch22` applies this file.
#
# What it creates: one Agent Runtime deployment, Sessions, one Agent
# Gateway, and the governance policy that enforces the 5,000-cent
# threshold. Memory Bank is deliberately absent: it is the most thoroughly
# specified managed-memory product in the chapter and also the least
# portable thing on this list, so adopting it is a separate decision with
# its own exit-cost note.
#
# What it costs while it exists: model inference, Runtime CPU and memory,
# Sessions, Code Execution, evaluation and judge inference, retrieval and
# vector services, logging and trace storage, network, and tools. There is
# a free Runtime tier. Pin the pricing page and its date in every cost
# benchmark; the meters were reorganized in 2026.
#
# The enterprise-control matrix is not uniform across subservices. VPC
# Service Controls, CMEK, residency, access transparency, and access
# approval support vary by component. Check each data path separately.

terraform {
  required_version = ">= 1.9"
}

variable "project" {
  type = string
}

variable "region" {
  type        = string
  description = "Residency is a decision, not an inherited default."
}

variable "approval_threshold_cents" {
  type    = number
  default = 5000
}

resource "google_agent_runtime" "support_agent" {
  project              = var.project
  location             = var.region
  display_name         = "northstar-support-agent"
  container_uri        = var.container_uri
  max_operation_seconds = 604800
}

resource "google_agent_sessions" "support_agent" {
  project  = var.project
  location = var.region
  runtime  = google_agent_runtime.support_agent.id
}

resource "google_agent_gateway" "tools" {
  project  = var.project
  location = var.region
  protocol = "mcp"
  backend  = google_agent_runtime.support_agent.id
}

resource "google_agent_governance_policy" "refunds" {
  project  = var.project
  location = var.region
  gateway  = google_agent_gateway.tools.id
  rule     = "deny issue_refund when amount_cents >= ${var.approval_threshold_cents}"
}

variable "container_uri" {
  type        = string
  description = "Digest-pinned image."
}

output "tool_endpoint" {
  value = google_agent_gateway.tools.mcp_url
}
