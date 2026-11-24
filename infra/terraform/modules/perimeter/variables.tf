variable "env" {
  type = string
}

variable "access_policy_id" {
  description = "Org-level Access Context Manager policy id (numeric). SLB-owned: PLACEHOLDER:Q-10:vpcsc_access_policy_id until supplied."
  type        = string
}

variable "project_numbers" {
  description = "Project numbers to place inside the perimeter."
  type        = list(string)
}

variable "restricted_services" {
  description = "Services protected by the perimeter (docs/03 §5)."
  type        = list(string)
  default = [
    "aiplatform.googleapis.com",    # Agent Platform / Agent Runtime, Sessions, Memory Bank
    "agentregistry.googleapis.com", # Agent Registry (GA, VPC-SC supported)
    "alloydb.googleapis.com",
    "bigquery.googleapis.com",
    "storage.googleapis.com",
    "pubsub.googleapis.com",
    "secretmanager.googleapis.com",
    "cloudkms.googleapis.com",
    "dlp.googleapis.com", # Sensitive Data Protection
    "modelarmor.googleapis.com",
    "run.googleapis.com",
    "artifactregistry.googleapis.com",
    "logging.googleapis.com",
  ]
}

variable "enforce" {
  description = "false = dry-run spec only (recommended first, docs/12 §4 stage 8). true = also enforce the same spec."
  type        = bool
  default     = false
}

variable "access_level_names" {
  description = "Optional access levels (full resource names) allowed into the perimeter (e.g. CI runners, break-glass)."
  type        = list(string)
  default     = []
}
