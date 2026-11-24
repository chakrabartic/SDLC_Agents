output "registry_uri" {
  description = "Regional registry referenced by the egress gateway."
  value       = "//agentregistry.googleapis.com/projects/${var.project_id}/locations/${var.region}"
}

output "mcp_server_resources" {
  description = "server -> generated McpServer resource name (projects/<p>/locations/<r>/mcpServers/<id>)."
  value       = { for k, s in google_agent_registry_service.mcp : k => s.registry_resource }
}

output "mcp_server_ids" {
  description = "server -> MCP server id (for --mcp-server / mcp_server_id). The id differs from service_id."
  value       = { for k, s in google_agent_registry_service.mcp : k => regex("[^/]+$", s.registry_resource) }
}

output "agent_resources" {
  value = { for k, s in google_agent_registry_service.agent : k => s.registry_resource }
}

output "agent_ids" {
  value = { for k, s in google_agent_registry_service.agent : k => regex("[^/]+$", s.registry_resource) }
}

output "endpoint_resources" {
  value = { for k, s in google_agent_registry_service.endpoint : k => s.registry_resource }
}

output "endpoint_ids" {
  value = { for k, s in google_agent_registry_service.endpoint : k => regex("[^/]+$", s.registry_resource) }
}

output "tools_by_server" {
  description = "server -> [{name, read_only}] parsed from the registered tool specs (input for tool-level CEL)."
  value = {
    for k, spec in local.specs : k => [
      for t in spec.tools : { name = t.name, read_only = try(t.annotations.readOnlyHint, false) }
    ]
  }
}
