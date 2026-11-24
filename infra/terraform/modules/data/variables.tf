variable "project_id" {
  type = string
}

variable "project_number" {
  type = string
}

variable "region" {
  type = string
}

variable "env" {
  type = string
}

variable "labels" {
  type    = map(string)
  default = {}
}

# ---------------------------------------------------------------- network (AlloyDB PSC endpoint)
variable "network_id" {
  type = string
}

variable "workloads_subnet_id" {
  type = string
}

variable "private_dns_zone_name" {
  type = string
}

variable "private_dns_domain" {
  type = string
}

# ---------------------------------------------------------------- CMEK
variable "kms_key_alloydb" {
  type = string
}

variable "kms_key_gcs" {
  type = string
}

variable "kms_key_bigquery" {
  type = string
}

variable "kms_key_pubsub" {
  type = string
}

# ---------------------------------------------------------------- AlloyDB
variable "alloydb_cpu_count" {
  type    = number
  default = 2
}

variable "alloydb_availability_type" {
  description = "ZONAL for dev, REGIONAL (HA) for stage/prod."
  type        = string
  default     = "REGIONAL"
}

variable "alloydb_read_pool_nodes" {
  description = "0 disables the read pool."
  type        = number
  default     = 0
}

variable "alloydb_deletion_protection" {
  type    = bool
  default = true
}

variable "dr_region" {
  description = "Cross-region replica region (residency sign-off required, Q-09). null or a placeholder disables the secondary cluster."
  type        = string
  default     = null
}

variable "dr_kms_key_alloydb" {
  description = "CMEK key in dr_region for the secondary cluster (CMEK keys are regional). null disables the secondary."
  type        = string
  default     = null
}

# ---------------------------------------------------------------- GCS
variable "bucket_prefix" {
  description = "Optional prefix to keep bucket names globally unique (docs/10 §7 names: pm-agent-artifacts-<env>, pm-agent-ledger-exports-<env>)."
  type        = string
  default     = ""
}

variable "raw_payload_retention_days" {
  description = "Encrypted raw payloads under raw/ are deleted after this many days (docs/10 §7.1: 180)."
  type        = number
  default     = 180
}

variable "ledger_export_retention_days" {
  description = "Bucket Lock retention for signed ledger exports (docs/10 §7.1: 7 years)."
  type        = number
  default     = 2557
}

variable "lock_ledger_export_retention" {
  description = "Lock the retention policy (IRREVERSIBLE: objects cannot be deleted before expiry and the policy cannot be shortened)."
  type        = bool
  default     = true
}

# ---------------------------------------------------------------- IAM wiring (members are 'serviceAccount:...')
variable "artifact_bucket_object_admins" {
  type    = list(string)
  default = []
}

variable "artifact_bucket_object_creators" {
  type    = list(string)
  default = []
}

variable "artifact_bucket_object_viewers" {
  type    = list(string)
  default = []
}

variable "ledger_export_object_creators" {
  description = "Only mcp-ledger writes signed segment exports."
  type        = list(string)
  default     = []
}

variable "bigquery_data_editors" {
  type    = list(string)
  default = []
}

variable "bigquery_data_viewers" {
  type    = list(string)
  default = []
}

variable "alloydb_clients" {
  description = "Members that connect to AlloyDB with IAM database authentication (roles/alloydb.client + databaseUser). Schema grants are Postgres-level (migrations)."
  type        = list(string)
  default     = []
}

variable "topic_publishers" {
  description = "topic short name -> publisher members."
  type        = map(list(string))
  default     = {}
}

variable "subscription_subscribers" {
  description = "subscription short name -> subscriber members."
  type        = map(list(string))
  default     = {}
}

variable "pubsub_service_agent" {
  description = "service-<PNUM>@gcp-sa-pubsub.iam.gserviceaccount.com (needed for dead-letter forwarding)."
  type        = string
}

# ---------------------------------------------------------------- Secret Manager (docs/10 §7)
variable "secrets" {
  description = "secret_id -> accessor members (owning workload SAs only). Real values are added out-of-band, never through Terraform."
  type        = map(list(string))
  default     = {}
}

variable "secret_placeholder_values" {
  description = "secret_id -> PLACEHOLDER token written as the bootstrap version (so Cloud Run secret env / External Secrets resolve and the app fails closed on the token until the real value is added as a newer version). Tokens are public, never credentials."
  type        = map(string)
  default     = {}
  validation {
    condition     = alltrue([for v in values(var.secret_placeholder_values) : can(regex("^PLACEHOLDER:Q-[0-9]{2}:[a-z][a-z0-9_]*$", v))])
    error_message = "Bootstrap secret values must be PLACEHOLDER:<Q-id>:<snake_case_name> tokens — real secrets never go through Terraform state."
  }
}

variable "kms_key_secretmanager" {
  description = "CMEK key for Secret Manager replicas (null = Google-managed)."
  type        = string
  default     = null
}
