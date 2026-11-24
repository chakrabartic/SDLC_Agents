output "sev1_metric_names" {
  value = { for k, m in google_logging_metric.sev1 : k => m.name }
}

output "sev1_alert_policy_ids" {
  value = { for k, a in google_monitoring_alert_policy.sev1 : k => a.id }
}

output "dashboard_id" {
  value = google_monitoring_dashboard.pm_agent.id
}
