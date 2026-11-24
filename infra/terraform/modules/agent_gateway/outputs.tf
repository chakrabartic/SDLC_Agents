output "egress_gateway_id" {
  description = "projects/<p>/locations/<r>/agentGateways/<name> — agent_gateway_config.agent_to_anywhere_config.agent_gateway for every Agent Runtime deploy."
  value       = google_network_services_agent_gateway.egress.id
}

output "ingress_gateway_id" {
  description = "agent_gateway_config.client_to_agent_config.agent_gateway for pm-agent-core / signal-ingestion."
  value       = google_network_services_agent_gateway.ingress.id
}

output "egress_gateway_name" {
  value = google_network_services_agent_gateway.egress.name
}

output "ingress_gateway_name" {
  value = google_network_services_agent_gateway.ingress.name
}

output "egress_gateway_root_certificates" {
  description = "Gateway CA roots: bake into custom-container (BYOC) Agent Runtime images (deploy/agent_runtime/README.md)."
  value       = try(google_network_services_agent_gateway.egress.agent_gateway_card[0].root_certificates, null)
}

output "egress_gateway_service_extensions_sa" {
  value = try(google_network_services_agent_gateway.egress.agent_gateway_card[0].service_extensions_service_account, null)
}

output "connectivity_template_id" {
  value = google_network_services_agent_connectivity_template.egress.id
}
