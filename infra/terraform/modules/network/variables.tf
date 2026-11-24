variable "project_id" {
  description = "Project that hosts the VPC (Shared VPC host project in stage/prod is Q-10)."
  type        = string
}

variable "region" {
  description = "Single region for runtime, data and Agent Gateway (docs/08 §6)."
  type        = string
}

variable "env" {
  description = "Environment name (dev | stage | prod)."
  type        = string
}

variable "network_name" {
  description = "VPC name."
  type        = string
  default     = "pm-agent"
}

variable "workloads_cidr" {
  description = "Primary range for Cloud Run Direct VPC egress, GKE nodes and PSC endpoints."
  type        = string
  default     = "10.10.0.0/20"
}

variable "gke_pods_cidr" {
  description = "Secondary range for GKE Autopilot pods."
  type        = string
  default     = "10.20.0.0/16"
}

variable "gke_services_cidr" {
  description = "Secondary range for GKE Autopilot services."
  type        = string
  default     = "10.30.0.0/20"
}

variable "psc_runtime_cidr" {
  description = "Subnet for the Agent Runtime PSC-interface network attachment (psc_interface_config)."
  type        = string
  default     = "10.40.0.0/26"
}

variable "psc_gateway_cidr" {
  description = "Subnet for the Agent Gateway egress network attachment (connectivity template, ALL_TRAFFIC)."
  type        = string
  default     = "10.40.0.64/26"
}

variable "proxy_only_cidr" {
  description = "Proxy-only subnet for regional internal Application LBs (GKE Gateway gke-l7-rilb in front of mcp-correlation)."
  type        = string
  default     = "10.50.0.0/23"
}

variable "nat_static_ip_count" {
  description = "Static Cloud NAT egress IPs, so SLB ServiceNow/ADO conditional-access allow-lists can pin them (docs/03 §5)."
  type        = number
  default     = 2
}

variable "private_dns_domain" {
  description = "Private DNS zone for internal names (AlloyDB PSC endpoint, mcp-correlation ILB). Must end with a dot."
  type        = string
  default     = "pm-agent.internal."
}

variable "labels" {
  description = "Extra resource labels."
  type        = map(string)
  default     = {}
}
