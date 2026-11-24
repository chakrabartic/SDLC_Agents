output "pm_prompt_template_name" {
  description = "projects/<p>/locations/<r>/templates/ma-pm-prompt (ingress gateway, request + response)."
  value       = "projects/${var.project_id}/locations/${var.region}/templates/${google_model_armor_template.pm_prompt.template_id}"
}

output "tool_response_template_name" {
  description = "projects/<p>/locations/<r>/templates/ma-tool-response (egress gateway, request + response)."
  value       = "projects/${var.project_id}/locations/${var.region}/templates/${google_model_armor_template.tool_response.template_id}"
}

output "sdp_inspect_template" {
  value = google_data_loss_prevention_inspect_template.tool_payloads.id
}

output "sdp_deidentify_template" {
  value = google_data_loss_prevention_deidentify_template.tool_payloads.id
}
