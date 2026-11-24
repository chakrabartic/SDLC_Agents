output "enforcement_mode" {
  description = "DRY_RUN (audit-only) or ENFORCED — the IAP authz extension on the egress gateway."
  value       = var.dry_run ? "DRY_RUN" : "ENFORCED"
}

output "policy_model" {
  value = var.policy_model
}

output "applied" {
  description = "false until apply_enabled and every agent principal is resolved (policies are rendered only)."
  value       = local.can_apply
}

output "pairs" {
  description = "(identity, destination) pairs derived from the authorization matrix (docs/08 §3.2)."
  value       = { for k, p in local.pairs : k => { identity = local.aid[p.identity], destination = p.dest, mode = p.mode, tools = p.tools } }
}

output "rendered_pair_policies" {
  description = "Per-pair policy JSON in the docs/08 §3.3 shape."
  value       = { for k, f in local_file.pair_policy : k => f.content }
}

output "rendered_dir" {
  value = "${local.rendered_dir}/${var.env}"
}

output "iap_authz_extension_id" {
  value = google_network_services_authz_extension.iap.id
}
