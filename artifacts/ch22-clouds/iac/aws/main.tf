# artifacts/ch22-clouds/iac/aws/main.tf
#
# Opt-in. Nothing in `make demo-ch22` applies this file: the demo runs the
# scorecard against the mock adapter, so it works with no account. This
# overlay is validated by parsing, and applying it creates real resources
# that cost real money for as long as they exist.
#
# What it creates: one AgentCore Runtime endpoint, one Gateway fronting the
# six Northstar tools, one workload identity, and the Cedar policy that
# enforces the 5,000-cent approval threshold at the Gateway rather than in
# agent code.
#
# What it costs while it exists: active Runtime CPU and memory, Gateway
# calls and indexed tools, Identity credential and token requests, Memory
# operations and storage, model inference, and telemetry storage. A
# runtime-only estimate is routinely off by a large multiple. Measure with
# a trace-linked experiment before you quote anyone a figure.

terraform {
  required_version = ">= 1.9"
}

variable "region" {
  type        = string
  description = "Residency is a decision, not an inherited default."
}

variable "approval_threshold_cents" {
  type        = number
  default     = 5000
  description = "Identical on every platform, or the comparison is void."
}

resource "aws_bedrockagentcore_runtime" "support_agent" {
  name             = "northstar-support-agent"
  region           = var.region
  container_uri    = var.container_uri
  session_idle_seconds = 900
  session_max_seconds  = 28800
  invocation_source    = "gateway_only"
}

resource "aws_bedrockagentcore_gateway" "tools" {
  name       = "northstar-gateway"
  region     = var.region
  protocol   = "mcp"
  target_arn = aws_bedrockagentcore_runtime.support_agent.arn
}

resource "aws_bedrockagentcore_identity" "workload" {
  name        = "northstar-support-agent"
  region      = var.region
  on_behalf_of = true
}

resource "aws_bedrockagentcore_policy" "refunds" {
  name       = "northstar-refund-threshold"
  region     = var.region
  gateway_id = aws_bedrockagentcore_gateway.tools.id
  mode       = "enforce"
  cedar      = <<-CEDAR
    forbid (principal, action == Action::"issue_refund", resource)
    when { context.amount_cents >= ${var.approval_threshold_cents} };
  CEDAR
}

variable "container_uri" {
  type        = string
  description = "Digest-pinned image. A tag is a dependency you did not choose."
}

output "tool_endpoint" {
  value = aws_bedrockagentcore_gateway.tools.mcp_url
}
