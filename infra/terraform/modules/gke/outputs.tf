output "cluster_name" {
  value = google_container_cluster.autopilot.name
}

output "cluster_id" {
  value = google_container_cluster.autopilot.id
}

output "workload_pool" {
  value = "${var.project_id}.svc.id.goog"
}

output "event_gateway_static_ip_name" {
  description = "Use in the Ingress annotation kubernetes.io/ingress.global-static-ip-name."
  value       = google_compute_global_address.event_gateway.name
}

output "event_gateway_static_ip" {
  value = google_compute_global_address.event_gateway.address
}

output "event_gateway_security_policy" {
  description = "Use in the BackendConfig spec.securityPolicy.name."
  value       = google_compute_security_policy.event_gateway.name
}
