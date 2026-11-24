# Composition of every PM Agent module for one environment. envs/<env> are thin roots (provider + backend + tfvars).
# SLB-specific unknowns are PLACEHOLDER:<Q-id>:<name> tokens (docs/15): tolerated in dev (dependent resources are
# skipped / rendered only), rejected in stage/prod (envs/<env> output precondition, strict_placeholders = true).

variable "env" {
  type = string
  validation {
    condition     = contains(["dev", "stage", "prod"], var.env)
    error_message = "env must be dev, stage or prod."
  }
}

variable "project_id" {
  type = string
}

variable "region" {
  type    = string
  default = "us-central1"
}

variable "labels" {
  type    = map(string)
  default = {}
}

variable "config_file" {
  description = "Path to config/pm_agent.yaml (custom SDP infoTypes and IdP group mapping are read from it). null = repo default."
  type        = string
  default     = null
}

# ---------------------------------------------------------------- SLB inputs (placeholders until answered)
variable "org_id" {
  description = "PLACEHOLDER:Q-10:gcp_org_id — Agent Identity trust domain, workforce pool, PAB."
  type        = string
}

variable "vpcsc_access_policy_id" {
  description = "PLACEHOLDER:Q-10:vpcsc_access_policy_id"
  type        = string
}

variable "idp_issuer_uri" {
  description = "PLACEHOLDER:Q-08:entra_oidc_issuer_uri"
  type        = string
}

variable "idp_client_id" {
  description = "PLACEHOLDER:Q-08:entra_wif_client_id"
  type        = string
}

variable "event_gateway_hostname" {
  description = "Public webhook hostname for signal-event-gateway — PLACEHOLDER:Q-10:event_gateway_hostname."
  type        = string
}

variable "dr_region" {
  description = "AlloyDB cross-region replica region — PLACEHOLDER:Q-09:dr_region (residency sign-off) or null."
  type        = string
  default     = null
}

# ---------------------------------------------------------------- Agent Runtime / identities
variable "agent_identity_trust_domain" {
  description = "Override the Agent Identity trust domain (default agents.global.org-<org_id>.system.id.goog; current docs also show agents.global.proj-<PNUM>.system.id.goog)."
  type        = string
  default     = null
}

variable "reasoning_engine_ids" {
  description = "Agent Runtime resource ids (docs/08 §3.1 uses the display names; Agent Runtime may assign numeric ids at create — feed the real ids back after the first deploy)."
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
  description = "Phase switch: false until the three Agent Runtime deployments exist (then registry entry for commit-executor, policy binding and reasoning-engine service-agent grants are created)."
  type        = bool
  default     = false
}

# ---------------------------------------------------------------- policy rollout
variable "access_policy_dry_run" {
  description = "Agent Gateway IAM evaluation in DRY_RUN (audit-only). Default true; enforce only after the soak (docs/12 §4)."
  type        = bool
  default     = true
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
  description = "false = VPC-SC dry-run spec only."
  type        = bool
  default     = false
}

variable "enable_pab" {
  type    = bool
  default = false
}

# ---------------------------------------------------------------- sizing / safety
variable "kms_protection_level" {
  type    = string
  default = "HSM"
}

variable "bucket_prefix" {
  type    = string
  default = ""
}

variable "lock_ledger_retention" {
  description = "Lock ledger log bucket + Bucket Lock on exports (IRREVERSIBLE)."
  type        = bool
  default     = true
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
  description = "AlloyDB, GKE and Cloud Run deletion protection."
  type        = bool
  default     = true
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
  description = "Set Cloud Run functional_type = FUNCTIONAL_TYPE_MCP_SERVER (Preview; auto-registers a second registry entry without tool specs)."
  type        = bool
  default     = false
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
  description = "Additional APIs to enable (e.g. agentidentity.googleapis.com once allow-listed — Preview)."
  type        = list(string)
  default     = []
}
