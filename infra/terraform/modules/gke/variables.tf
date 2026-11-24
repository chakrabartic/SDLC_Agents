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

variable "network_id" {
  type = string
}

variable "subnetwork_id" {
  type = string
}

variable "pods_range_name" {
  type = string
}

variable "services_range_name" {
  type = string
}

variable "master_ipv4_cidr_block" {
  description = "/28 for the private control plane."
  type        = string
  default     = "172.16.0.0/28"
}

variable "master_authorized_cidrs" {
  description = "Internal ranges allowed to reach the private control-plane endpoint (CI private pool, bastion)."
  type        = map(string)
  default     = {}
}

variable "release_channel" {
  type    = string
  default = "REGULAR"
}

variable "database_encryption_key" {
  description = "CMEK for application-layer Secrets encryption (null = Google-managed)."
  type        = string
  default     = null
}

variable "deletion_protection" {
  type    = bool
  default = true
}

variable "workload_identity_bindings" {
  description = "GSA email -> {namespace, ksa}: grants roles/iam.workloadIdentityUser to <project>.svc.id.goog[ns/ksa] (KSA annotated with iam.gke.io/gcp-service-account)."
  type = map(object({
    namespace = string
    ksa       = string
  }))
  default = {}
}

variable "event_gateway_allowed_cidrs" {
  description = "Optional vendor webhook source allow-list for Cloud Armor (empty = allow all, rely on signature verification + rate limits)."
  type        = list(string)
  default     = []
}

variable "event_gateway_rate_limit_per_minute" {
  type    = number
  default = 600
}
