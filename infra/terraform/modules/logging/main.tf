# Locked Cloud Logging bucket `pm-agent-ledger` (docs/07 §8.1, docs/10 §7): mirror of every ledger entry and the
# periodic chain-head anchors, Log Analytics enabled, linked BigQuery dataset for SQL audits (docs/07 §8.2), CMEK.

locals {
  ledger_filter = "logName=\"projects/${var.project_id}/logs/${var.ledger_log_name}\""
  sink_filter   = join(" OR ", concat(["(${local.ledger_filter})"], [for f in var.extra_sink_filters : "(${f})"]))
}

resource "google_logging_project_bucket_config" "ledger" {
  project          = var.project_id
  location         = var.region
  bucket_id        = var.bucket_id
  description      = "PM Agent ledger mirror + chain-head anchors (append-only evidence; retention locked outside dev)."
  retention_days   = var.retention_days
  locked           = var.locked
  enable_analytics = true

  dynamic "cmek_settings" {
    for_each = var.kms_key_name == null ? [] : [var.kms_key_name]
    content {
      kms_key_name = cmek_settings.value
    }
  }
}

resource "google_logging_linked_dataset" "ledger" {
  link_id     = var.linked_dataset_id
  bucket      = google_logging_project_bucket_config.ledger.id
  description = "Log Analytics link for governance SQL over the ledger mirror (docs/07 §8.2)."
}

resource "google_logging_project_sink" "ledger_mirror" {
  project                = var.project_id
  name                   = "pm-agent-ledger-mirror"
  destination            = "logging.googleapis.com/projects/${var.project_id}/locations/${var.region}/buckets/${google_logging_project_bucket_config.ledger.bucket_id}"
  filter                 = local.sink_filter
  unique_writer_identity = true
  description            = "Routes ledger mirror entries (and optional audit streams) into the locked bucket."
}

# Same-project bucket destinations do not strictly need this grant; it keeps the sink working if the bucket is later
# moved to a central audit project.
resource "google_project_iam_member" "sink_writer" {
  project = var.project_id
  role    = "roles/logging.bucketWriter"
  member  = google_logging_project_sink.ledger_mirror.writer_identity
}
