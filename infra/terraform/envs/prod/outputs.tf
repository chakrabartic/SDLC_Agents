# Strict environments (stage/prod) refuse to plan while SLB placeholders remain (mirrors pm_agent.placeholders
# strict mode). A failed output precondition fails `terraform plan` / `apply`.
output "unresolved_placeholders" {
  value = module.platform.unresolved_placeholders

  precondition {
    condition     = !var.strict_placeholders || length(module.platform.unresolved_placeholders) == 0
    error_message = "Unresolved SLB placeholders in strict environment ${var.env}: ${join(", ", module.platform.unresolved_placeholders)} (see docs/15)."
  }
}

output "agent_runtime_deploy_config" {
  value = module.platform.agent_runtime_deploy_config
}

output "agent_principals" {
  value = module.platform.agent_principals
}

output "access_policy_enforcement_mode" {
  value = module.platform.access_policy_enforcement_mode
}

output "access_policies_applied" {
  value = module.platform.access_policies_applied
}

output "access_policy_pairs" {
  value = module.platform.access_policy_pairs
}

output "rendered_policy_dir" {
  value = module.platform.rendered_policy_dir
}

output "vpcsc_perimeter" {
  value = module.platform.vpcsc_perimeter
}

output "cloud_run_uris" {
  value = module.platform.cloud_run_uris
}

output "service_accounts" {
  value = module.platform.service_accounts
}

output "mcp_server_ids" {
  value = module.platform.mcp_server_ids
}

output "endpoint_ids" {
  value = module.platform.endpoint_ids
}

output "nat_egress_ips" {
  value = module.platform.nat_egress_ips
}

output "kms" {
  value = module.platform.kms
}

output "data" {
  value = module.platform.data
}

output "k8s_overlay_values" {
  value = module.platform.k8s_overlay_values
}

output "egress_gateway_root_certificates" {
  value = module.platform.egress_gateway_root_certificates
}

output "ci_service_account_email" {
  value = module.platform.ci_service_account_email
}
