# Data stores (docs/10 §7): AlloyDB (PSC-only, CMEK, IAM DB auth, backups + PITR, optional read pool and
# cross-region secondary), GCS artifacts + Bucket-Locked ledger exports, BigQuery pm_agent_analytics,
# Pub/Sub topics with dead-letter topics. All CMEK-protected.

locals {
  # Bucket names are global; docs/10 names are kept and an optional prefix avoids collisions.
  artifacts_bucket      = "${var.bucket_prefix}pm-agent-artifacts-${var.env}"
  ledger_exports_bucket = "${var.bucket_prefix}pm-agent-ledger-exports-${var.env}"

  dr_enabled = var.dr_region != null && var.dr_kms_key_alloydb != null && !startswith(coalesce(var.dr_region, "x"), "PLACEHOLDER:")

  topics = toset(["signal-change-hints", "signal-ingested", "problem-updated", "ledger-events"])

  # subscription -> source topic (consumers per docs/03 §3.1 and docs/11)
  subscriptions = {
    "signal-change-hints-ingestion" = "signal-change-hints" # drained by signal-ingestion (AID-INGEST)
    "signal-ingested-correlation"   = "signal-ingested"     # correlation-engine (GKE)
    "problem-updated-workbench"     = "problem-updated"     # pm-workbench refresh
    "ledger-events-workbench"       = "ledger-events"       # pm-workbench approvals/audit refresh
  }

  publisher_grants = merge([
    for t, members in var.topic_publishers : { for m in members : "${t}|${m}" => { topic = t, member = m } }
  ]...)

  subscriber_grants = merge([
    for s, members in var.subscription_subscribers : { for m in members : "${s}|${m}" => { sub = s, member = m } }
  ]...)

  bucket_grants = merge(
    { for m in var.artifact_bucket_object_admins : "artifacts|roles/storage.objectAdmin|${m}" => { bucket = "artifacts", role = "roles/storage.objectAdmin", member = m } },
    { for m in var.artifact_bucket_object_creators : "artifacts|roles/storage.objectCreator|${m}" => { bucket = "artifacts", role = "roles/storage.objectCreator", member = m } },
    { for m in var.artifact_bucket_object_viewers : "artifacts|roles/storage.objectViewer|${m}" => { bucket = "artifacts", role = "roles/storage.objectViewer", member = m } },
    { for m in var.ledger_export_object_creators : "exports|roles/storage.objectCreator|${m}" => { bucket = "exports", role = "roles/storage.objectCreator", member = m } },
  )

  bq_grants = merge(
    { for m in var.bigquery_data_editors : "roles/bigquery.dataEditor|${m}" => { role = "roles/bigquery.dataEditor", member = m } },
    { for m in var.bigquery_data_viewers : "roles/bigquery.dataViewer|${m}" => { role = "roles/bigquery.dataViewer", member = m } },
  )

  alloydb_grants = merge([
    for m in var.alloydb_clients : {
      "roles/alloydb.client|${m}"                    = { role = "roles/alloydb.client", member = m }
      "roles/alloydb.databaseUser|${m}"              = { role = "roles/alloydb.databaseUser", member = m }
      "roles/serviceusage.serviceUsageConsumer|${m}" = { role = "roles/serviceusage.serviceUsageConsumer", member = m }
    }
  ]...)
}

# ================================================================= AlloyDB (db pm_agent; schemas via migrations)
resource "google_alloydb_cluster" "primary" {
  project             = var.project_id
  cluster_id          = "pm-agent-${var.env}"
  location            = var.region
  database_version    = "POSTGRES_16"
  deletion_protection = var.alloydb_deletion_protection
  labels              = var.labels

  psc_config {
    psc_enabled = true # private connectivity only (docs/10 §7)
  }

  encryption_config {
    kms_key_name = var.kms_key_alloydb
  }

  continuous_backup_config {
    enabled              = true
    recovery_window_days = 14
    encryption_config {
      kms_key_name = var.kms_key_alloydb
    }
  }

  automated_backup_policy {
    enabled       = true
    location      = var.region
    backup_window = "3600s"
    labels        = var.labels

    weekly_schedule {
      days_of_week = ["MONDAY", "TUESDAY", "WEDNESDAY", "THURSDAY", "FRIDAY", "SATURDAY", "SUNDAY"]
      start_times {
        hours = 2
      }
    }

    quantity_based_retention {
      count = 14
    }

    encryption_config {
      kms_key_name = var.kms_key_alloydb
    }
  }
}

resource "google_alloydb_instance" "primary" {
  cluster           = google_alloydb_cluster.primary.name
  instance_id       = "primary"
  instance_type     = "PRIMARY"
  availability_type = var.alloydb_availability_type
  labels            = var.labels

  machine_config {
    cpu_count = var.alloydb_cpu_count
  }

  database_flags = {
    "alloydb.iam_authentication" = "on" # per-service IAM DB users; no static passwords in workloads
  }

  client_connection_config {
    require_connectors = false
    ssl_config {
      ssl_mode = "ENCRYPTED_ONLY"
    }
  }

  psc_instance_config {
    allowed_consumer_projects = [var.project_number]
  }
}

resource "google_alloydb_instance" "read_pool" {
  count         = var.alloydb_read_pool_nodes > 0 ? 1 : 0
  cluster       = google_alloydb_cluster.primary.name
  instance_id   = "read-pool"
  instance_type = "READ_POOL"
  labels        = var.labels

  read_pool_config {
    node_count = var.alloydb_read_pool_nodes
  }

  machine_config {
    cpu_count = var.alloydb_cpu_count
  }

  database_flags = {
    "alloydb.iam_authentication" = "on"
  }

  psc_instance_config {
    allowed_consumer_projects = [var.project_number]
  }

  depends_on = [google_alloydb_instance.primary]
}

# PSC endpoint in our VPC -> AlloyDB primary service attachment, plus a private DNS name.
resource "google_compute_address" "alloydb_psc" {
  project      = var.project_id
  name         = "pm-agent-${var.env}-alloydb-psc"
  region       = var.region
  address_type = "INTERNAL"
  subnetwork   = var.workloads_subnet_id
  labels       = var.labels
}

resource "google_compute_forwarding_rule" "alloydb_psc" {
  project               = var.project_id
  name                  = "pm-agent-${var.env}-alloydb-psc"
  region                = var.region
  network               = var.network_id
  ip_address            = google_compute_address.alloydb_psc.id
  load_balancing_scheme = ""
  target                = google_alloydb_instance.primary.psc_instance_config[0].service_attachment_link
  labels                = var.labels
}

resource "google_dns_record_set" "alloydb_primary" {
  project      = var.project_id
  managed_zone = var.private_dns_zone_name
  name         = "alloydb-primary.${var.private_dns_domain}"
  type         = "A"
  ttl          = 300
  rrdatas      = [google_compute_address.alloydb_psc.address]
}

# Optional cross-region secondary (DR, docs/12 §7). Needs a DR-region CMEK key and residency sign-off (Q-09).
resource "google_alloydb_cluster" "secondary" {
  count               = local.dr_enabled ? 1 : 0
  project             = var.project_id
  cluster_id          = "pm-agent-${var.env}-dr"
  location            = var.dr_region
  cluster_type        = "SECONDARY"
  database_version    = google_alloydb_cluster.primary.database_version
  deletion_protection = var.alloydb_deletion_protection
  labels              = var.labels

  psc_config {
    psc_enabled = true
  }

  encryption_config {
    kms_key_name = var.dr_kms_key_alloydb
  }

  continuous_backup_config {
    enabled = false
  }

  secondary_config {
    primary_cluster_name = google_alloydb_cluster.primary.name
  }

  depends_on = [google_alloydb_instance.primary]
}

resource "google_alloydb_instance" "secondary" {
  count         = local.dr_enabled ? 1 : 0
  cluster       = google_alloydb_cluster.secondary[0].name
  instance_id   = "secondary"
  instance_type = "SECONDARY"
  labels        = var.labels

  machine_config {
    cpu_count = var.alloydb_cpu_count
  }

  psc_instance_config {
    allowed_consumer_projects = [var.project_number]
  }
}

resource "google_project_iam_member" "alloydb" {
  for_each = local.alloydb_grants
  project  = var.project_id
  role     = each.value.role
  member   = each.value.member
}

# ================================================================= Cloud Storage
resource "google_storage_bucket" "artifacts" {
  project                     = var.project_id
  name                        = local.artifacts_bucket
  location                    = upper(var.region)
  storage_class               = "STANDARD"
  uniform_bucket_level_access = true
  public_access_prevention    = "enforced"
  force_destroy               = false
  labels                      = var.labels

  versioning {
    enabled = true
  }

  encryption {
    default_kms_key_name = var.kms_key_gcs
  }

  # Encrypted raw source payloads: 180 days, then delete (provenance keeps hash + permalink; docs/10 §7.1).
  lifecycle_rule {
    condition {
      age            = var.raw_payload_retention_days
      matches_prefix = ["raw/"]
    }
    action {
      type = "Delete"
    }
  }

  lifecycle_rule {
    condition {
      days_since_noncurrent_time = 30
    }
    action {
      type = "Delete"
    }
  }
}

# WORM evidence for audits: daily signed ledger segment exports under a (locked) Bucket Lock retention policy.
# NOTE: retention policies and Object Versioning are mutually exclusive — versioning stays off here.
resource "google_storage_bucket" "ledger_exports" {
  project                     = var.project_id
  name                        = local.ledger_exports_bucket
  location                    = upper(var.region)
  storage_class               = "STANDARD"
  uniform_bucket_level_access = true
  public_access_prevention    = "enforced"
  force_destroy               = false
  labels                      = merge(var.labels, { classification = "restricted-audit" })

  encryption {
    default_kms_key_name = var.kms_key_gcs
  }

  retention_policy {
    retention_period = var.ledger_export_retention_days * 86400
    is_locked        = var.lock_ledger_export_retention
  }
}

resource "google_storage_bucket_iam_member" "grants" {
  for_each = local.bucket_grants
  bucket   = each.value.bucket == "artifacts" ? google_storage_bucket.artifacts.name : google_storage_bucket.ledger_exports.name
  role     = each.value.role
  member   = each.value.member
}

# ================================================================= BigQuery
resource "google_bigquery_dataset" "analytics" {
  project                    = var.project_id
  dataset_id                 = "pm_agent_analytics"
  friendly_name              = "PM Agent analytics (${var.env})"
  description                = "Signals/problems facts, trace links, KPI tables, eval results, BigQueryAgentAnalyticsPlugin events (docs/10 §7, docs/12 §5)."
  location                   = var.region
  delete_contents_on_destroy = false
  labels                     = var.labels

  default_encryption_configuration {
    kms_key_name = var.kms_key_bigquery
  }
}

resource "google_bigquery_dataset_iam_member" "grants" {
  for_each   = local.bq_grants
  project    = var.project_id
  dataset_id = google_bigquery_dataset.analytics.dataset_id
  role       = each.value.role
  member     = each.value.member
}

# ================================================================= Pub/Sub (+ dead-letter topics)
resource "google_pubsub_topic" "topic" {
  for_each                   = local.topics
  project                    = var.project_id
  name                       = each.key
  kms_key_name               = var.kms_key_pubsub
  message_retention_duration = "604800s"
  labels                     = var.labels

  message_storage_policy {
    allowed_persistence_regions = [var.region]
  }
}

resource "google_pubsub_topic" "dlq" {
  for_each                   = local.topics
  project                    = var.project_id
  name                       = "${each.key}-dlq"
  kms_key_name               = var.kms_key_pubsub
  message_retention_duration = "604800s"
  labels                     = merge(var.labels, { role = "dead-letter" })

  message_storage_policy {
    allowed_persistence_regions = [var.region]
  }
}

resource "google_pubsub_subscription" "sub" {
  for_each                   = local.subscriptions
  project                    = var.project_id
  name                       = each.key
  topic                      = google_pubsub_topic.topic[each.value].id
  ack_deadline_seconds       = 60
  message_retention_duration = "604800s"
  enable_message_ordering    = false
  labels                     = var.labels

  expiration_policy {
    ttl = "" # never expire
  }

  retry_policy {
    minimum_backoff = "10s"
    maximum_backoff = "600s"
  }

  dead_letter_policy {
    dead_letter_topic     = google_pubsub_topic.dlq[each.value].id
    max_delivery_attempts = 5
  }
}

# Hold subscriptions so dead-lettered messages are retained for inspection/replay.
resource "google_pubsub_subscription" "dlq_hold" {
  for_each                   = local.topics
  project                    = var.project_id
  name                       = "${each.key}-dlq-hold"
  topic                      = google_pubsub_topic.dlq[each.key].id
  ack_deadline_seconds       = 600
  message_retention_duration = "604800s"
  labels                     = merge(var.labels, { role = "dead-letter" })

  expiration_policy {
    ttl = ""
  }
}

# Dead-letter forwarding needs the Pub/Sub service agent to publish to DLQs and subscribe to the sources.
resource "google_pubsub_topic_iam_member" "dlq_publisher" {
  for_each = local.topics
  project  = var.project_id
  topic    = google_pubsub_topic.dlq[each.key].name
  role     = "roles/pubsub.publisher"
  member   = "serviceAccount:${var.pubsub_service_agent}"
}

resource "google_pubsub_subscription_iam_member" "dlq_forwarder" {
  for_each     = local.subscriptions
  project      = var.project_id
  subscription = google_pubsub_subscription.sub[each.key].name
  role         = "roles/pubsub.subscriber"
  member       = "serviceAccount:${var.pubsub_service_agent}"
}

resource "google_pubsub_topic_iam_member" "publishers" {
  for_each = local.publisher_grants
  project  = var.project_id
  topic    = google_pubsub_topic.topic[each.value.topic].name
  role     = "roles/pubsub.publisher"
  member   = each.value.member
}

resource "google_pubsub_subscription_iam_member" "subscribers" {
  for_each     = local.subscriber_grants
  project      = var.project_id
  subscription = google_pubsub_subscription.sub[each.value.sub].name
  role         = "roles/pubsub.subscriber"
  member       = each.value.member
}

# ================================================================= Secret Manager (per-server third-party credentials)
locals {
  secret_grants = merge([
    for sid, members in var.secrets : { for m in members : "${sid}|${m}" => { secret = sid, member = m } }
  ]...)
}

resource "google_secret_manager_secret" "this" {
  for_each  = var.secrets
  project   = var.project_id
  secret_id = each.key
  labels    = var.labels

  replication {
    user_managed {
      replicas {
        location = var.region
        dynamic "customer_managed_encryption" {
          for_each = var.kms_key_secretmanager == null ? [] : [var.kms_key_secretmanager]
          content {
            kms_key_name = customer_managed_encryption.value
          }
        }
      }
    }
  }
}

resource "google_secret_manager_secret_iam_member" "accessor" {
  for_each  = local.secret_grants
  project   = var.project_id
  secret_id = google_secret_manager_secret.this[each.value.secret].secret_id
  role      = "roles/secretmanager.secretAccessor"
  member    = each.value.member
}

# Bootstrap versions holding PLACEHOLDER tokens (public; the app treats them as "not configured" and fails closed).
# Rotate in the real value by ADDING a version out-of-band (gcloud secrets versions add). Disable — do not destroy —
# the bootstrap version afterwards: a destroyed version would be re-created by Terraform as the newest version.
resource "google_secret_manager_secret_version" "placeholder" {
  for_each    = { for k, v in var.secret_placeholder_values : k => v if contains(keys(var.secrets), k) }
  secret      = google_secret_manager_secret.this[each.key].id
  secret_data = each.value

  lifecycle {
    ignore_changes = [enabled, secret_data]
  }
}
