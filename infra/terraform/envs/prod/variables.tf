# Identical in every env; values live in terraform.tfvars. PLACEHOLDER:<Q-id>:<name> = SLB input pending (docs/15).

variable "env" {
  type = string
}

variable "project_id" {
  type = string
  validation {
    condition     = !startswith(var.project_id, "PLACEHOLDER:")
    error_message = "project_id is unresolved (Q-10: SLB GCP projects). Supply the real project id in terraform.tfvars."
  }
}

variable "region" {
  type    = string
  default = "us-central1"
}

variable "strict_placeholders" {
  type    = bool
  default = true
}

variable "org_id" {
  type = string
}

variable "vpcsc_access_policy_id" {
  type = string
}

variable "idp_issuer_uri" {
  type = string
}

variable "idp_client_id" {
  type = string
}

variable "event_gateway_hostname" {
  type = string
}

variable "dr_region" {
  type    = string
  default = null
}

variable "agent_identity_trust_domain" {
  type    = string
  default = null
}

variable "reasoning_engine_ids" {
  type = object({
    pm_agent_core    = string
    signal_ingestion = string
    commit_executor  = string
  })
  default = {
    pm_agent_core    = "pm-agent-core"
    signal_ingestion = "signal-ingestion"
    commit_executor  = "commit-executor"
  }
}

variable "agent_runtime_deployed" {
  type    = bool
  default = false
}

variable "access_policy_dry_run" {
  type    = bool
  default = true
}

variable "policy_model" {
  type    = string
  default = "IAP_ALLOW_V1"
}

variable "policy_apply_mode" {
  type    = string
  default = "native"
}

variable "vpcsc_enforce" {
  type    = bool
  default = false
}

variable "enable_pab" {
  type    = bool
  default = false
}

variable "kms_protection_level" {
  type    = string
  default = "HSM"
}

variable "bucket_prefix" {
  type    = string
  default = ""
}

variable "lock_ledger_retention" {
  type    = bool
  default = true
}

variable "ledger_retention_days" {
  type    = number
  default = 2557
}

variable "alloydb_cpu_count" {
  type    = number
  default = 2
}

variable "alloydb_availability_type" {
  type    = string
  default = "REGIONAL"
}

variable "alloydb_read_pool_nodes" {
  type    = number
  default = 0
}

variable "deletion_protection" {
  type    = bool
  default = true
}

variable "nat_static_ip_count" {
  type    = number
  default = 2
}

variable "gke_master_authorized_cidrs" {
  type    = map(string)
  default = {}
}

variable "event_gateway_allowed_cidrs" {
  type    = list(string)
  default = []
}

variable "mcp_functional_type_enabled" {
  type    = bool
  default = false
}

variable "notification_channel_ids" {
  type    = list(string)
  default = []
}

variable "cloudbuild_repository" {
  type    = string
  default = null
}

variable "extra_apis" {
  type    = list(string)
  default = []
}
