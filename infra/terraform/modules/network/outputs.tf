output "network_id" {
  description = "VPC id (projects/<p>/global/networks/<name>)."
  value       = google_compute_network.vpc.id
}

output "network_name" {
  value = google_compute_network.vpc.name
}

output "network_self_link" {
  value = google_compute_network.vpc.self_link
}

output "workloads_subnet_id" {
  value = google_compute_subnetwork.workloads.id
}

output "workloads_subnet_name" {
  value = google_compute_subnetwork.workloads.name
}

output "gke_pods_range_name" {
  value = "gke-pods"
}

output "gke_services_range_name" {
  value = "gke-services"
}

output "agent_runtime_network_attachment_id" {
  description = "Use as psc_interface_config.network_attachment for every Agent Runtime deployment."
  value       = google_compute_network_attachment.agent_runtime.id
}

output "agent_gateway_network_attachment_id" {
  description = "Used by the Agent Gateway egress connectivity template."
  value       = google_compute_network_attachment.agent_gateway.id
}

output "nat_egress_ips" {
  description = "Static egress IPs to give SLB for ADO/ServiceNow conditional-access allow-lists."
  value       = google_compute_address.nat[*].address
}

output "correlation_ilb_address" {
  value = google_compute_address.correlation_ilb.address
}

output "correlation_ilb_address_name" {
  description = "Address name for the k8s annotation networking.gke.io/load-balancer-ip-addresses."
  value       = google_compute_address.correlation_ilb.name
}

output "private_dns_zone_name" {
  value = google_dns_managed_zone.private.name
}

output "private_dns_domain" {
  value = var.private_dns_domain
}

output "correlation_mcp_hostname" {
  value = trimsuffix("mcp-correlation.${var.private_dns_domain}", ".")
}
