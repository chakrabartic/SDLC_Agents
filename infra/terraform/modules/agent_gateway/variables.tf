variable "project_id" {
  type = string
}

variable "project_number" {
  type = string
}

variable "region" {
  description = "Gateways, registry, Model Armor templates and agents must share this region."
  type        = string
}

variable "env" {
  type = string
}

variable "labels" {
  type    = map(string)
  default = {}
}

variable "registry_uri" {
  description = "//agentregistry.googleapis.com/projects/<p>/locations/<r>"
  type        = string
}

variable "egress_network_attachment_id" {
  description = "PSC network attachment for VPC egress (required for VPC-SC enforcement on the gateway)."
  type        = string
}

variable "vpc_egress" {
  description = "ALL_TRAFFIC from day one (NFR-2): VPC-SC is enforced only for gateways created after 2026-09-08 with a connectivity template."
  type        = string
  default     = "ALL_TRAFFIC"
}

variable "dns_peering_domains" {
  description = "Private DNS domains the gateway resolves through our VPC (e.g. pm-agent.internal. for mcp-correlation)."
  type        = list(string)
  default     = []
}

variable "dns_peering_target_network" {
  description = "projects/<p>/global/networks/<n> hosting the private zones."
  type        = string
  default     = null
}

variable "egress_trust_config" {
  description = "Optional Certificate Manager trust config (private CA for internal MCP endpoints such as mcp-correlation)."
  type        = string
  default     = null
}

variable "model_armor_enabled" {
  type    = bool
  default = true
}

variable "ingress_model_armor_template" {
  description = "ma-pm-prompt (request + response) on the Client-to-Agent gateway."
  type        = string
}

variable "egress_model_armor_template" {
  description = "ma-tool-response (request + response) on the Agent-to-Anywhere gateway."
  type        = string
}

variable "reasoning_engine_service_agent_exists" {
  description = "service-<PNUM>@gcp-sa-aiplatform-re exists only after the first Agent Runtime deploy; set true in the second apply."
  type        = bool
  default     = false
}
