# Identities (docs/08 §3): one service account per Cloud Run service (SA-<service>), one GSA per GKE workload
# (Workload Identity Federation for GKE; KSAs are annotated with iam.gke.io/gcp-service-account) and the Workforce
# Identity Federation pool for SLB humans (IAP). Per-server secrets live in modules/data (Secret Manager is a store).
# Agent identities (AID-CORE / AID-INGEST / AID-COMMIT) are NOT created here: Agent Runtime issues them at first
# deploy (identity_type = AGENT_IDENTITY) — see deploy/agent_runtime/README.md.

locals {
  # Deterministic emails (known at plan time) so callers can use them in for_each keys.
  run_sa_ids = { for s in var.cloud_run_services : s => substr("sa-${s}", 0, 30) }
  gke_sa_ids = { for k, w in var.gke_workloads : k => substr("sa-gke-${k}", 0, 30) }

  run_sa_emails = { for s, id in local.run_sa_ids : s => "${id}@${var.project_id}.iam.gserviceaccount.com" }
  gke_sa_emails = { for k, id in local.gke_sa_ids : k => "${id}@${var.project_id}.iam.gserviceaccount.com" }

  all_sa_emails = merge(local.run_sa_emails, local.gke_sa_emails)

  baseline = merge([
    for wl, email in local.all_sa_emails : {
      for r in var.baseline_roles : "${wl}|${r}" => { email = email, role = r }
    }
  ]...)

  org_known        = !startswith(var.org_id, "PLACEHOLDER:")
  idp_known        = !startswith(var.idp_issuer_uri, "PLACEHOLDER:") && !startswith(var.idp_client_id, "PLACEHOLDER:")
  create_workforce = local.org_known && local.idp_known
}

resource "google_service_account" "cloud_run" {
  for_each     = local.run_sa_ids
  project      = var.project_id
  account_id   = each.value
  display_name = "PM Agent ${var.env} — ${each.key} (Cloud Run)"
  description  = "SA-${upper(each.key)}: runtime identity of Cloud Run service ${each.key}; holds only that server's credentials (docs/08 §3.1)."
}

resource "google_service_account" "gke" {
  for_each     = local.gke_sa_ids
  project      = var.project_id
  account_id   = each.value
  display_name = "PM Agent ${var.env} — ${each.key} (GKE)"
  description  = "GSA for KSA ${var.gke_workloads[each.key].namespace}/${var.gke_workloads[each.key].ksa} via Workload Identity Federation for GKE."
}

resource "google_project_iam_member" "baseline" {
  for_each = local.baseline
  project  = var.project_id
  role     = each.value.role
  member   = "serviceAccount:${each.value.email}"

  depends_on = [google_service_account.cloud_run, google_service_account.gke]
}

# ---------------------------------------------------------------- Workforce Identity Federation (humans via IAP)
resource "google_iam_workforce_pool" "slb" {
  count             = local.create_workforce ? 1 : 0
  parent            = "organizations/${var.org_id}"
  location          = "global"
  workforce_pool_id = var.workforce_pool_id
  display_name      = "SLB workforce (PM Agent)"
  description       = "SLB IdP users for the PM Workbench (IAP) and approvals (docs/07 §5)."
  session_duration  = "3600s"
}

resource "google_iam_workforce_pool_provider" "slb_oidc" {
  count             = local.create_workforce ? 1 : 0
  location          = "global"
  workforce_pool_id = google_iam_workforce_pool.slb[0].workforce_pool_id
  provider_id       = "slb-entra-oidc"
  display_name      = "SLB IdP (OIDC)"
  attribute_mapping = {
    "google.subject"      = "assertion.sub"
    "google.groups"       = "assertion.groups"
    "google.display_name" = "assertion.name"
    "attribute.email"     = "assertion.email"
  }

  oidc {
    issuer_uri = var.idp_issuer_uri
    client_id  = var.idp_client_id
    web_sso_config {
      response_type             = "ID_TOKEN"
      assertion_claims_behavior = "ONLY_ID_TOKEN_CLAIMS"
    }
  }
}
