variable "project_id" {
  type = string
}

variable "region" {
  description = "Key ring location; must match the location of the CMEK-protected resources."
  type        = string
}

variable "env" {
  type = string
}

variable "protection_level" {
  description = "HSM for signing keys and CMEK (docs/10 §7: HSM-backed signing)."
  type        = string
  default     = "HSM"
  validation {
    condition     = contains(["HSM", "SOFTWARE"], var.protection_level)
    error_message = "protection_level must be HSM or SOFTWARE."
  }
}

variable "cmek_keys" {
  description = "One symmetric CMEK key per consuming service (limits blast radius)."
  type        = set(string)
  default     = ["alloydb", "gcs", "bigquery", "pubsub", "logging", "secretmanager", "artifactregistry", "gke", "agent-runtime"]
}

variable "cmek_rotation_period" {
  type    = string
  default = "7776000s" # 90 days
}

variable "cmek_encrypter_decrypters" {
  description = "CMEK key short name -> { static label -> member } granted roles/cloudkms.cryptoKeyEncrypterDecrypter. Labels keep for_each keys static even when member emails are only known after apply."
  type        = map(map(string))
  default     = {}
}

variable "approval_token_signers" {
  description = "Members allowed to SIGN with approval-token-signer. Must be exactly the mcp-ledger SA (docs/07 §6, docs/08 §10)."
  type        = list(string)
}

variable "verifier_report_signers" {
  description = "Members allowed to SIGN with verifier-report-signer. Must be exactly the mcp-verifier SA."
  type        = list(string)
}

variable "signing_key_viewers" {
  description = "Every component verifies tokens/reports: roles/cloudkms.viewer + roles/cloudkms.publicKeyViewer on BOTH signing keys."
  type        = list(string)
  default     = []
}

variable "sdp_wrap_decrypters" {
  description = "Members allowed to unwrap the SDP pseudonymization key (the SDP service agent)."
  type        = list(string)
  default     = []
}

variable "labels" {
  type    = map(string)
  default = {}
}
