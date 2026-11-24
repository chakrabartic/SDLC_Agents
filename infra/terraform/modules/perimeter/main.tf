# VPC Service Controls perimeter (docs/03 §5, NFR-2). Always created with an explicit DRY-RUN spec; the enforced
# `status` mirrors the spec only when var.enforce = true (after the dry-run soak shows zero unexpected violations).
# The org access policy is SLB-owned (Q-10): while it is a placeholder nothing is created.

locals {
  policy_known = !startswith(var.access_policy_id, "PLACEHOLDER:")
  resources    = [for n in var.project_numbers : "projects/${n}"]
  perimeter    = "pm_agent_${var.env}"
}

resource "google_access_context_manager_service_perimeter" "this" {
  count          = local.policy_known ? 1 : 0
  parent         = "accessPolicies/${var.access_policy_id}"
  name           = "accessPolicies/${var.access_policy_id}/servicePerimeters/${local.perimeter}"
  title          = local.perimeter
  description    = "PM Agent ${var.env}: Agent Platform, Agent Registry, data stores, KMS, SDP, Model Armor, Cloud Run, Artifact Registry"
  perimeter_type = "PERIMETER_TYPE_REGULAR"

  use_explicit_dry_run_spec = true

  spec {
    resources           = local.resources
    restricted_services = var.restricted_services
    access_levels       = var.access_level_names

    vpc_accessible_services {
      enable_restriction = true
      allowed_services   = ["RESTRICTED-SERVICES"]
    }
  }

  dynamic "status" {
    for_each = var.enforce ? [1] : []
    content {
      resources           = local.resources
      restricted_services = var.restricted_services
      access_levels       = var.access_level_names

      vpc_accessible_services {
        enable_restriction = true
        allowed_services   = ["RESTRICTED-SERVICES"]
      }
    }
  }
}
