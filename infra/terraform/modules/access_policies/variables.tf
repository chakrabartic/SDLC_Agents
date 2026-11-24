variable "project_id" {
  description = "Gateway project (registry + policies live with the gateway)."
  type        = string
}

variable "region" {
  type = string
}

variable "env" {
  type = string
}

# ---------------------------------------------------------------- rollout switches
variable "dry_run" {
  description = "true => the gateway's IAP authz extension runs with iamEnforcementMode = DRY_RUN (audit-only; would-be denials are logged). Flip to false only after the soak shows zero unexpected denials (docs/03 §6, docs/12 §4)."
  type        = bool
  default     = true
}

variable "policy_model" {
  description = "IAP_ALLOW_V1 = roles/iap.egressor allow policies on registry resources exactly as docs/08 §3.3 (iapPolicyVersion V1). UAP_V2 = IAM Unified Access Policy (iapPolicyVersion V2, recommended by current Agent Gateway docs) generated from the same matrix."
  type        = string
  default     = "IAP_ALLOW_V1"
  validation {
    condition     = contains(["IAP_ALLOW_V1", "UAP_V2"], var.policy_model)
    error_message = "policy_model must be IAP_ALLOW_V1 or UAP_V2."
  }
}

variable "apply_mode" {
  description = "native = google_iap_agent_registry_*_iam_policy resources; gcloud = terraform_data running `gcloud iap web set-iam-policy` with the rendered per-destination files (fallback while the Preview surface churns)."
  type        = string
  default     = "native"
  validation {
    condition     = contains(["native", "gcloud"], var.apply_mode)
    error_message = "apply_mode must be native or gcloud."
  }
}

variable "egress_gateway_id" {
  description = "projects/<p>/locations/<r>/agentGateways/<name> (target of the IAP REQUEST_AUTHZ policy and of the UAP binding)."
  type        = string
}

variable "iap_authz_timeout" {
  type    = string
  default = "1s"
}

# ---------------------------------------------------------------- identities (docs/08 §3.1)
variable "agent_principals" {
  description = "Agent Identity principals of the three Agent Runtime deployments. While they contain PLACEHOLDER tokens (org id / project number / engine ids) policies are rendered but not applied."
  type = object({
    core   = string # AID-CORE   (pm-agent-core)
    ingest = string # AID-INGEST (signal-ingestion)
    commit = string # AID-COMMIT (commit-executor)
  })
}

# ---------------------------------------------------------------- registry inputs (from modules/agent_registry)
variable "mcp_server_ids" {
  type = map(string)
}

variable "mcp_server_resources" {
  type = map(string)
}

variable "tools_by_server" {
  type = map(list(object({ name = string, read_only = bool })))
}

variable "endpoint_ids" {
  description = "Registry endpoints -> id: commit-executor (A2A) + essential platform endpoints (all three agents need those once bound to an enforcing gateway)."
  type        = map(string)
  default     = {}
}

variable "endpoint_resources" {
  type    = map(string)
  default = {}
}

# ---------------------------------------------------------------- authorization matrix (docs/08 §3.2) — defaults == approved spec
variable "read_servers" {
  type    = list(string)
  default = ["mcp-ado", "mcp-servicenow", "mcp-pagerduty", "mcp-msgraph", "mcp-kb", "mcp-figma"]
}

variable "internal_servers" {
  type    = list(string)
  default = ["mcp-store", "mcp-correlation", "mcp-verifier"]
}

variable "core_internal_excluded_tools" {
  description = "AID-CORE: internal servers ✅ except these tools (\"no put_signals\"). Rendered as an explicit allow-list (deny-by-default for tools added later)."
  type        = list(string)
  default     = ["put_signals"]
}

variable "core_ledger_tools" {
  type    = list(string)
  default = ["propose", "query", "get_action"]
}

variable "ingest_internal_tools" {
  type = map(list(string))
  default = {
    "mcp-store"       = ["put_signals"]
    "mcp-correlation" = ["assign_candidates"]
    "mcp-verifier"    = ["redact"]
  }
}

variable "commit_internal_tools" {
  type = map(list(string))
  default = {
    "mcp-verifier" = ["verify_subject", "policy_evaluate"]
  }
}

variable "commit_ledger_tools" {
  description = "AID-COMMIT on mcp-ledger (src/pm_agent/hitl/catalog.py: PROTOTYPE_RELEASE_INTERNAL, ROADMAP_TRACE_LINK run on the ledger)."
  type        = list(string)
  default     = ["consume_token", "record", "get_action", "release_validation_package", "record_trace_links"]
}

variable "write_tools" {
  description = "AID-COMMIT on write servers: exact tool names only (docs/08 §3.3; action catalog in src/pm_agent/hitl/catalog.py)."
  type        = map(list(string))
  default = {
    "mcp-ado-write"          = ["create_work_item", "add_relations"]
    "mcp-kb-write"           = ["publish_article"]
    "mcp-figma-write"        = ["post_comment"]
    "mcp-msgraph-write"      = ["send_mail"]
    "svc-prototype-renderer" = ["release_prototype_spec"]
  }
}

variable "a2a_executor_endpoint" {
  description = "Registry key of the commit-executor endpoint (AID-CORE -> A2A `execute`)."
  type        = string
  default     = "commit-executor"
}

variable "apply_enabled" {
  description = "Bind policies only after the Agent Runtime deployments exist and their engine ids are fed back (phase 3 of the apply order)."
  type        = bool
  default     = false
}

# ---------------------------------------------------------------- rendering
variable "rendered_dir" {
  description = "Where rendered policy JSON files are written for review / gcloud fallback (git-ignored)."
  type        = string
  default     = null
}

# ---------------------------------------------------------------- UAP prerequisites + Principal Access Boundary
variable "manage_access_policy_binding_org_policy" {
  description = "UAP_V2 only: turn off the managed constraint that blocks access-policy bindings on this project (needs orgpolicy.policyAdmin)."
  type        = bool
  default     = false
}

variable "enable_pab" {
  description = "Principal Access Boundary ceiling for principals of this project (org-scoped; docs/08 §2). Off until Q-10 is resolved and the principal set for agent identities is confirmed."
  type        = bool
  default     = false
}

variable "org_id" {
  type    = string
  default = "PLACEHOLDER:Q-10:gcp_org_id"
}
