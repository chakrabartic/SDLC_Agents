output "perimeter_name" {
  description = "Perimeter resource name, or null while the access policy id is a placeholder (Q-10)."
  value       = try(google_access_context_manager_service_perimeter.this[0].name, null)
}

output "mode" {
  value = var.enforce ? "ENFORCED (+dry-run spec)" : "DRY_RUN"
}

output "skipped_reason" {
  value = startswith(var.access_policy_id, "PLACEHOLDER:") ? "access_policy_id unresolved: ${var.access_policy_id}" : null
}
