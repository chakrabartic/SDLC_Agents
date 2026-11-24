variable "project_id" {
  type = string
}

variable "env" {
  type = string
}

variable "ledger_log_name" {
  description = "Log the ledger mirror is written to (modules/logging)."
  type        = string
  default     = "pm-agent-ledger"
}

variable "notification_channel_ids" {
  description = "Monitoring notification channels for SEV-1 alerts (on-call routing is environment-specific)."
  type        = list(string)
  default     = []
}

variable "workbench_service_name" {
  type    = string
  default = "pm-workbench"
}

variable "ledger_service_name" {
  type    = string
  default = "mcp-ledger"
}

variable "verifier_service_name" {
  type    = string
  default = "mcp-verifier"
}

variable "workbench_availability_goal" {
  description = "docs/12 §5.1: 99.5 % monthly."
  type        = number
  default     = 0.995
}

variable "ledger_availability_goal" {
  description = "docs/12 §5.1: ledger write availability 99.9 %."
  type        = number
  default     = 0.999
}
