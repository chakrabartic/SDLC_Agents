output "artifact_registry_repository" {
  description = "<region>-docker.pkg.dev/<project>/<repo>"
  value       = "${var.region}-docker.pkg.dev/${var.project_id}/${google_artifact_registry_repository.images.repository_id}"
}

output "ci_service_account_email" {
  description = "Value for cloudbuild.yaml _AGENT_RUNTIME_SA (replaces PLACEHOLDER:Q-10:deploy_service_account)."
  value       = google_service_account.ci.email
}
