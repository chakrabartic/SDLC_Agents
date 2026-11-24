variable "project_id" {
  type = string
}

variable "region" {
  description = "Bucket location (regional for residency, docs/08 §6). The CMEK key must be in the same location."
  type        = string
}

variable "bucket_id" {
  type    = string
  default = "pm-agent-ledger"
}

variable "retention_days" {
  description = "Ledger + exports are Restricted-audit, 7 years (docs/10 §7.1)."
  type        = number
  default     = 2557
}

variable "locked" {
  description = "Lock the bucket retention (IRREVERSIBLE: retention can no longer be changed; bucket deletable only when empty)."
  type        = bool
  default     = true
}

variable "kms_key_name" {
  description = "CMEK key for the log bucket (null = Google-managed)."
  type        = string
  default     = null
}

variable "linked_dataset_id" {
  description = "BigQuery dataset id created by the Log Analytics link (letters, digits, underscores)."
  type        = string
  default     = "pm_agent_ledger_logs"
}

variable "ledger_log_name" {
  description = "Log name mcp-ledger writes ledger mirror entries and chain-head anchors to (docs/07 §8.1)."
  type        = string
  default     = "pm-agent-ledger"
}

variable "extra_sink_filters" {
  description = "Additional filters to mirror into the locked bucket (e.g. Agent Gateway audit logs, admin activity)."
  type        = list(string)
  default     = []
}
