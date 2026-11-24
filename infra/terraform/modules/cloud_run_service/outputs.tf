output "name" {
  value = google_cloud_run_v2_service.this.name
}

output "uri" {
  description = "Service URL (https://<name>-<hash>.<region>.run.app)."
  value       = google_cloud_run_v2_service.this.uri
}

output "id" {
  value = google_cloud_run_v2_service.this.id
}

output "service_account_email" {
  value = var.service_account_email
}
