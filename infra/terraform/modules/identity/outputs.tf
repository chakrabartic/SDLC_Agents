output "cloud_run_sa_emails" {
  description = "Cloud Run service -> service account email."
  value       = local.run_sa_emails
}

output "gke_sa_emails" {
  description = "GKE workload -> GSA email (use in the KSA annotation iam.gke.io/gcp-service-account)."
  value       = local.gke_sa_emails
}

output "cloud_run_sa_members" {
  value = { for k, e in local.run_sa_emails : k => "serviceAccount:${e}" }
}

output "gke_sa_members" {
  value = { for k, e in local.gke_sa_emails : k => "serviceAccount:${e}" }
}

output "service_accounts_ready" {
  description = "Depend on this before binding IAM to the service accounts."
  value       = concat([for s in google_service_account.cloud_run : s.unique_id], [for s in google_service_account.gke : s.unique_id])
}

output "workforce_pool_name" {
  description = "locations/global/workforcePools/<id>, or null while org/IdP details are placeholders (Q-10/Q-08)."
  value       = try(google_iam_workforce_pool.slb[0].name, null)
}

output "workforce_principal_set_prefix" {
  description = "Prefix for group principal sets, e.g. <prefix>/group/<idp group id>."
  value       = "principalSet://iam.googleapis.com/locations/global/workforcePools/${var.workforce_pool_id}"
}
