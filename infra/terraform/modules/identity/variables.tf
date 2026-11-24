variable "project_id" {
  type = string
}

variable "region" {
  type = string
}

variable "env" {
  type = string
}

variable "org_id" {
  description = "GCP organization id (Q-10). Workforce pools are org-scoped; skipped while this is a placeholder."
  type        = string
}

variable "cloud_run_services" {
  description = "Cloud Run services that get one dedicated service account each (docs/12 §2)."
  type        = set(string)
}

variable "gke_workloads" {
  description = "GKE workloads: name -> Kubernetes namespace/KSA. Each gets a GSA bound through Workload Identity Federation for GKE."
  type = map(object({
    namespace = string
    ksa       = string
  }))
  default = {}
}

variable "baseline_roles" {
  description = "Project roles every runtime service account needs (telemetry only — no data access)."
  type        = list(string)
  default     = ["roles/logging.logWriter", "roles/monitoring.metricWriter", "roles/cloudtrace.agent"]
}

variable "workforce_pool_id" {
  description = "Workforce Identity Federation pool for SLB humans. docs/07 §6 shows workforcePools/slb, but pool ids must be 6-63 chars; reuse an existing SLB pool id here if Q-08 says so."
  type        = string
  default     = "slb-pm-agent"
}

variable "idp_issuer_uri" {
  description = "OIDC issuer of the SLB IdP (Entra ID assumed) — PLACEHOLDER:Q-08:entra_oidc_issuer_uri."
  type        = string
}

variable "idp_client_id" {
  description = "OIDC client id registered in the SLB IdP for Workforce Identity Federation — PLACEHOLDER:Q-08:entra_wif_client_id."
  type        = string
}

variable "labels" {
  type    = map(string)
  default = {}
}
