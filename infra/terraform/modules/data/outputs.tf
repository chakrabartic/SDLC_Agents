output "alloydb_cluster_name" {
  value = google_alloydb_cluster.primary.name
}

output "alloydb_primary_instance_name" {
  description = "Instance URI for AlloyDB connectors (projects/<p>/locations/<r>/clusters/<c>/instances/primary)."
  value       = google_alloydb_instance.primary.name
}

output "alloydb_psc_endpoint_ip" {
  value = google_compute_address.alloydb_psc.address
}

output "alloydb_hostname" {
  value = trimsuffix("alloydb-primary.${var.private_dns_domain}", ".")
}

output "artifacts_bucket" {
  value = google_storage_bucket.artifacts.name
}

output "ledger_exports_bucket" {
  value = google_storage_bucket.ledger_exports.name
}

output "ledger_exports_retention_locked" {
  value = google_storage_bucket.ledger_exports.retention_policy[0].is_locked
}

output "analytics_dataset_id" {
  value = google_bigquery_dataset.analytics.dataset_id
}

output "topic_ids" {
  value = { for k, t in google_pubsub_topic.topic : k => t.id }
}

output "dead_letter_topic_ids" {
  value = { for k, t in google_pubsub_topic.dlq : k => t.id }
}

output "subscription_ids" {
  value = { for k, s in google_pubsub_subscription.sub : k => s.id }
}

output "secondary_cluster_name" {
  value = try(google_alloydb_cluster.secondary[0].name, null)
}

output "secret_ids" {
  description = "secret_id -> secret resource name (workloads get the NAME via env, never the value)."
  value       = { for k, s in google_secret_manager_secret.this : k => s.id }
}

output "secret_bootstrap_versions" {
  description = "Depend on this before deploying workloads that mount secrets (Cloud Run secret env / External Secrets)."
  value       = { for k, v in google_secret_manager_secret_version.placeholder : k => v.name }
}
