# Observability (docs/12 §5): log-based metrics for the hard safety SLOs, SEV-1 alert policies, request-based
# availability SLOs for the Workbench and the Ledger, and a minimal dashboard.
#
# Log contract (for the application code): structured JSON with `jsonPayload.event` set to one of
#   TOOL_CALL_DENIED | UNCITED_CLAIM_IN_APPROVED_OUTPUT | LEDGER_CHAIN_MISMATCH | COMMIT_WITHOUT_TOKEN
# (ledger mirror entries carry `jsonPayload.entry_type`, docs/07 §8.3).

locals {
  ledger_log = "logName=\"projects/${var.project_id}/logs/${var.ledger_log_name}\""

  sev1_metrics = {
    tool_call_denied = {
      display = "TOOL_CALL_DENIED from AID-CORE / AID-INGEST (must be 0 at write servers)"
      filter  = "(${local.ledger_log} AND jsonPayload.entry_type=\"TOOL_CALL_DENIED\") OR jsonPayload.event=\"TOOL_CALL_DENIED\""
    }
    uncited_claims_in_approved_outputs = {
      display = "Uncited claims in approved outputs (SLO = 0, any occurrence is SEV-1)"
      filter  = "jsonPayload.event=\"UNCITED_CLAIM_IN_APPROVED_OUTPUT\""
    }
    ledger_chain_mismatch = {
      display = "Ledger hash-chain mismatch (integrity job)"
      filter  = "jsonPayload.event=\"LEDGER_CHAIN_MISMATCH\""
    }
    commit_without_token = {
      display = "COMMIT without TOKEN_CONSUMED (nightly control query, AC9)"
      filter  = "jsonPayload.event=\"COMMIT_WITHOUT_TOKEN\""
    }
  }

  run_filter = "resource.type=\"cloud_run_revision\" AND metric.type=\"run.googleapis.com/request_count\""
}

resource "google_logging_metric" "sev1" {
  for_each = local.sev1_metrics
  project  = var.project_id
  name     = "pm_agent/${each.key}"
  filter   = each.value.filter

  metric_descriptor {
    metric_kind  = "DELTA"
    value_type   = "INT64"
    unit         = "1"
    display_name = each.value.display
  }
}

resource "google_monitoring_alert_policy" "sev1" {
  for_each              = local.sev1_metrics
  project               = var.project_id
  display_name          = "[SEV-1][${var.env}] ${each.value.display}"
  combiner              = "OR"
  notification_channels = var.notification_channel_ids
  severity              = "CRITICAL"

  conditions {
    display_name = each.value.display
    condition_threshold {
      filter          = "metric.type=\"logging.googleapis.com/user/${google_logging_metric.sev1[each.key].name}\" AND resource.type=\"cloud_run_revision\""
      comparison      = "COMPARISON_GT"
      threshold_value = 0
      duration        = "0s"

      aggregations {
        alignment_period     = "60s"
        per_series_aligner   = "ALIGN_SUM"
        cross_series_reducer = "REDUCE_SUM"
      }

      trigger {
        count = 1
      }
    }
  }

  documentation {
    content   = "Runbook: docs/12 §6. This control must stay at zero; investigate immediately and record the incident in the ledger."
    mime_type = "text/markdown"
  }
}

# ---------------------------------------------------------------- SLOs (docs/12 §5.1)
resource "google_monitoring_custom_service" "workbench" {
  project      = var.project_id
  service_id   = "pm-agent-${var.env}-workbench"
  display_name = "PM Workbench (${var.env})"
}

resource "google_monitoring_slo" "workbench_availability" {
  project         = var.project_id
  service         = google_monitoring_custom_service.workbench.service_id
  slo_id          = "availability"
  display_name    = "Workbench availability ${var.workbench_availability_goal * 100}% monthly"
  goal            = var.workbench_availability_goal
  calendar_period = "MONTH"

  request_based_sli {
    good_total_ratio {
      bad_service_filter   = "${local.run_filter} AND resource.label.\"service_name\"=\"${var.workbench_service_name}\" AND metric.label.\"response_code_class\"=\"5xx\""
      total_service_filter = "${local.run_filter} AND resource.label.\"service_name\"=\"${var.workbench_service_name}\""
    }
  }
}

resource "google_monitoring_custom_service" "ledger" {
  project      = var.project_id
  service_id   = "pm-agent-${var.env}-ledger"
  display_name = "HITL Ledger (${var.env})"
}

resource "google_monitoring_slo" "ledger_availability" {
  project             = var.project_id
  service             = google_monitoring_custom_service.ledger.service_id
  slo_id              = "availability"
  display_name        = "Ledger availability ${var.ledger_availability_goal * 100}% (rolling 30d)"
  goal                = var.ledger_availability_goal
  rolling_period_days = 30

  request_based_sli {
    good_total_ratio {
      bad_service_filter   = "${local.run_filter} AND resource.label.\"service_name\"=\"${var.ledger_service_name}\" AND metric.label.\"response_code_class\"=\"5xx\""
      total_service_filter = "${local.run_filter} AND resource.label.\"service_name\"=\"${var.ledger_service_name}\""
    }
  }
}

# ---------------------------------------------------------------- dashboard
resource "google_monitoring_dashboard" "pm_agent" {
  project = var.project_id
  dashboard_json = jsonencode({
    displayName = "PM Agent (${var.env}) — safety & availability"
    mosaicLayout = {
      columns = 12
      tiles = concat(
        [
          for i, k in sort(keys(local.sev1_metrics)) : {
            xPos   = (i % 2) * 6
            yPos   = floor(i / 2) * 4
            width  = 6
            height = 4
            widget = {
              title = local.sev1_metrics[k].display
              xyChart = {
                dataSets = [{
                  plotType = "STACKED_BAR"
                  timeSeriesQuery = {
                    timeSeriesFilter = {
                      filter      = "metric.type=\"logging.googleapis.com/user/pm_agent/${k}\" AND resource.type=\"cloud_run_revision\""
                      aggregation = { alignmentPeriod = "300s", perSeriesAligner = "ALIGN_SUM", crossSeriesReducer = "REDUCE_SUM" }
                    }
                  }
                }]
              }
            }
          }
        ],
        [
          for i, s in [var.workbench_service_name, var.ledger_service_name, var.verifier_service_name] : {
            xPos   = (i % 3) * 4
            yPos   = 8
            width  = 4
            height = 4
            widget = {
              title = "${s} requests by response class"
              xyChart = {
                dataSets = [{
                  plotType = "LINE"
                  timeSeriesQuery = {
                    timeSeriesFilter = {
                      filter      = "${local.run_filter} AND resource.label.\"service_name\"=\"${s}\""
                      aggregation = { alignmentPeriod = "300s", perSeriesAligner = "ALIGN_RATE", crossSeriesReducer = "REDUCE_SUM", groupByFields = ["metric.label.\"response_code_class\""] }
                    }
                  }
                }]
              }
            }
          }
        ],
      )
    }
  })

  depends_on = [google_logging_metric.sev1]
}
