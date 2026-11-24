# Agent Registry entries (docs/08 §2-3, docs/12 §3). Provider support CONFIRMED: google_agent_registry_service
# (hashicorp/google >= 7.39; Agent Registry GA release notes: "use Terraform to configure ... agents, MCP servers,
# endpoints, and bindings"). MCP servers are registered MANUALLY with a TOOL_SPEC so that tool annotations
# (readOnlyHint, the security contract of docs/08 §3.3) are present in the registry for tool-level policies.
# Cloud Run auto-registration (functional_type = MCP_SERVER) only records name + type and would create a second
# entry — see infra/README.md (UNCONFIRMED table).
#
# Registry entries and the IAM policies bound to them are created in the SAME apply (bindings to unregistered
# resources fail with NOT_FOUND).

locals {
  spec_dir = coalesce(var.tool_spec_dir, "${path.module}/toolspecs")
  specs    = { for k, _ in var.mcp_servers : k => jsondecode(file("${local.spec_dir}/${k}.json")) }
}

resource "google_agent_registry_service" "mcp" {
  for_each     = var.mcp_servers
  project      = var.project_id
  location     = var.region
  service_id   = each.key
  display_name = each.key
  description  = each.value.description

  interfaces {
    url              = each.value.url
    protocol_binding = "JSONRPC"
  }

  mcp_server_spec {
    type    = "TOOL_SPEC"
    content = jsonencode(local.specs[each.key])
  }
}

resource "google_agent_registry_service" "agent" {
  for_each     = var.agents
  project      = var.project_id
  location     = var.region
  service_id   = each.key
  display_name = each.key
  description  = each.value.description

  interfaces {
    url              = each.value.url
    protocol_binding = each.value.protocol_binding
  }

  agent_spec {
    type    = each.value.agent_card_json == null ? "NO_SPEC" : "A2A_AGENT_CARD"
    content = each.value.agent_card_json
  }
}

resource "google_agent_registry_service" "endpoint" {
  for_each     = var.endpoints
  project      = var.project_id
  location     = var.region
  service_id   = each.key
  display_name = each.key
  description  = each.value.description

  interfaces {
    url              = each.value.url
    protocol_binding = "HTTP_JSON"
  }

  endpoint_spec {
    type = "NO_SPEC"
  }
}
