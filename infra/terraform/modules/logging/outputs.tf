output "bucket_name" {
  description = "Full log bucket resource name."
  value       = google_logging_project_bucket_config.ledger.name
}

output "bucket_locked" {
  value = google_logging_project_bucket_config.ledger.locked
}

output "linked_bigquery_dataset" {
  description = "BigQuery dataset created by the Log Analytics link."
  value       = google_logging_linked_dataset.ledger.link_id
}

output "ledger_log_name" {
  description = "Log name the ledger writes to (configure mcp-ledger with this)."
  value       = var.ledger_log_name
}
