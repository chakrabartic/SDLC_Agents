variable "project_id" {
  type = string
}

variable "region" {
  description = "Templates must live in the same region as the Agent Gateways that use them."
  type        = string
}

variable "labels" {
  type    = map(string)
  default = {}
}

variable "enforcement_type" {
  description = "INSPECT_AND_BLOCK in every env per docs/08 §5 (use INSPECT_ONLY only for a short tuning window)."
  type        = string
  default     = "INSPECT_AND_BLOCK"
  validation {
    condition     = contains(["INSPECT_ONLY", "INSPECT_AND_BLOCK"], var.enforcement_type)
    error_message = "enforcement_type must be INSPECT_ONLY or INSPECT_AND_BLOCK."
  }
}

variable "pi_jailbreak_confidence" {
  description = "Prompt-injection & jailbreak threshold (docs/08 §5: MEDIUM_AND_ABOVE)."
  type        = string
  default     = "MEDIUM_AND_ABOVE"
}

variable "builtin_info_types" {
  description = "SDP infoTypes inspected on tool payloads (PII + SECURITY_DATA subset verified in docs/02 M2)."
  type        = list(string)
  default = [
    "PERSON_NAME", "EMAIL_ADDRESS", "PHONE_NUMBER",
    "AUTH_TOKEN", "AZURE_AUTH_TOKEN", "GCP_API_KEY", "JSON_WEB_TOKEN", "PASSWORD", "OAUTH_CLIENT_SECRET",
  ]
}

variable "custom_info_types" {
  description = "Custom regex infoTypes for secrets SDP has no detector for (config/pm_agent.yaml grounding.custom_infotypes)."
  type        = map(string)
  default     = {}
}

variable "modelarmor_service_agent" {
  description = "service-<PNUM>@gcp-sa-modelarmor.iam.gserviceaccount.com — needs DLP roles to use the SDP templates (advanced SDP mode)."
  type        = string
}
