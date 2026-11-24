variable "project_id" {
  type = string
}

variable "region" {
  type = string
}

variable "name" {
  description = "Service name (docs/12 §2), e.g. mcp-ado, mcp-ledger, pm-workbench."
  type        = string
}

variable "image" {
  description = "Initial image. CI (cloudbuild.yaml, deploy/Dockerfile) owns image rollout; Terraform ignores later image changes."
  type        = string
  default     = "us-docker.pkg.dev/cloudrun/container/hello"
}

variable "service_account_email" {
  description = "Dedicated per-service SA (SA-<service>)."
  type        = string
}

variable "ingress" {
  description = "Internal by default (docs/12 §2). Only pm-workbench / svc-prototype-renderer use ALL behind IAP."
  type        = string
  default     = "INGRESS_TRAFFIC_INTERNAL_ONLY"
  validation {
    condition     = contains(["INGRESS_TRAFFIC_INTERNAL_ONLY", "INGRESS_TRAFFIC_INTERNAL_LOAD_BALANCER", "INGRESS_TRAFFIC_ALL"], var.ingress)
    error_message = "Invalid ingress value."
  }
}

variable "iap_enabled" {
  description = "Cloud Run built-in IAP (MCP servers are IAP-protected destinations of Agent Gateway; Workbench uses IAP + Workforce IdF)."
  type        = bool
  default     = true
}

variable "functional_type" {
  description = "FUNCTIONAL_TYPE_MCP_SERVER registers the service in Agent Registry automatically (Preview, google-beta, immutable once set). null = manual registration via modules/agent_registry."
  type        = string
  default     = null
  validation {
    condition     = var.functional_type == null || contains(["FUNCTIONAL_TYPE_MCP_SERVER", "FUNCTIONAL_TYPE_AGENT"], coalesce(var.functional_type, "FUNCTIONAL_TYPE_MCP_SERVER"))
    error_message = "functional_type must be null, FUNCTIONAL_TYPE_MCP_SERVER or FUNCTIONAL_TYPE_AGENT."
  }
}

variable "invoker_members" {
  description = "Authoritative roles/run.invoker members. For IAP-protected services this is ONLY the IAP service agent (docs/08 §4). allUsers/allAuthenticatedUsers are rejected."
  type        = list(string)
  validation {
    condition     = !contains(var.invoker_members, "allUsers") && !contains(var.invoker_members, "allAuthenticatedUsers")
    error_message = "Public invokers (allUsers / allAuthenticatedUsers) are forbidden."
  }
}

variable "iap_accessors" {
  description = "Authoritative roles/iap.httpsResourceAccessor members on this service's IAP resource."
  type        = list(string)
  default     = []
  validation {
    condition     = !contains(var.iap_accessors, "allUsers") && !contains(var.iap_accessors, "allAuthenticatedUsers")
    error_message = "Public IAP accessors are forbidden."
  }
}

variable "min_instances" {
  type    = number
  default = 0
}

variable "max_instances" {
  type    = number
  default = 20
}

variable "cpu" {
  type    = string
  default = "1"
}

variable "memory" {
  type    = string
  default = "512Mi"
}

variable "cpu_idle" {
  description = "false = CPU always allocated (verifier, docs/12 §2)."
  type        = bool
  default     = true
}

variable "concurrency" {
  type    = number
  default = 80
}

variable "request_timeout" {
  type    = string
  default = "300s"
}

variable "session_affinity" {
  description = "true for pm-workbench (SSE)."
  type        = bool
  default     = false
}

variable "container_port" {
  type    = number
  default = 8080
}

variable "env_vars" {
  description = "Plain env vars. Secrets are passed by resource NAME (read at runtime with the service SA), never by value."
  type        = map(string)
  default     = {}
}

variable "vpc_subnetwork" {
  description = "Direct VPC egress subnetwork id (null = no VPC egress)."
  type        = string
  default     = null
}

variable "vpc_egress" {
  description = "ALL_TRAFFIC for SaaS connectors (Cloud NAT static IPs), PRIVATE_RANGES_ONLY for internal services."
  type        = string
  default     = "PRIVATE_RANGES_ONLY"
}

variable "labels" {
  type    = map(string)
  default = {}
}

variable "deletion_protection" {
  type    = bool
  default = true
}
