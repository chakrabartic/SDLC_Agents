variable "project_id" {
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

variable "repository_id" {
  description = "Artifact Registry repository (cloudbuild.yaml substitution _AR_REPO)."
  type        = string
  default     = "pm-agent"
}

variable "kms_key_name" {
  description = "CMEK for the repository (null = Google-managed)."
  type        = string
  default     = null
}

variable "runtime_service_account_emails" {
  description = "Runtime SAs the CI SA may deploy Cloud Run revisions as (roles/iam.serviceAccountUser on each)."
  type        = list(string)
  default     = []
}

variable "cloudbuild_repository" {
  description = "Cloud Build 2nd-gen repository resource (projects/<p>/locations/<r>/connections/<c>/repositories/<r>). null = no trigger."
  type        = string
  default     = null
}

variable "trigger_branch_regex" {
  type    = string
  default = "^main$"
}
