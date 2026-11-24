# Model Armor templates attached to Agent Gateway (docs/08 §5). Provider support CONFIRMED:
# google_model_armor_template (hashicorp/google >= 6.43, CHANGELOG_v6). Attachment to gateways is done in
# modules/agent_gateway via Service Extensions authz extensions (google_network_services_authz_extension +
# google_network_security_authz_policy, CONTENT_AUTHZ) as documented in
# docs.cloud.google.com/gemini-enterprise-agent-platform/govern/gateways/delegate-authorization.
#
#   ma-pm-prompt      ingress (Workbench -> pm-agent-core): PI & jailbreak (MEDIUM_AND_ABOVE), RAI, malicious URLs
#   ma-tool-response  egress  (agents -> tools): PI & jailbreak on tool payloads, malicious URLs, SDP ADVANCED
#                     mode with our inspect/de-identify templates (redaction of payloads that only trip SDP)

locals {
  parent = "projects/${var.project_id}/locations/${var.region}"
}

# ---------------------------------------------------------------- SDP templates used by ma-tool-response
resource "google_data_loss_prevention_inspect_template" "tool_payloads" {
  parent       = local.parent
  template_id  = "pm-agent-tool-payloads"
  display_name = "PM Agent tool payload inspection"
  description  = "PII + secrets on tool payloads (docs/06 §5, docs/08 §5)."

  inspect_config {
    min_likelihood = "POSSIBLE"

    dynamic "info_types" {
      for_each = var.builtin_info_types
      content {
        name = info_types.value
      }
    }

    dynamic "custom_info_types" {
      for_each = var.custom_info_types
      content {
        info_type {
          name = custom_info_types.key
        }
        regex {
          pattern = custom_info_types.value
        }
        likelihood = "LIKELY"
      }
    }
  }
}

resource "google_data_loss_prevention_deidentify_template" "tool_payloads" {
  parent       = local.parent
  template_id  = "pm-agent-tool-payloads-deid"
  display_name = "PM Agent tool payload de-identification"
  description  = "Replace findings with their infoType name (pseudonymization with sdp-wrap happens in mcp-verifier)."

  deidentify_config {
    info_type_transformations {
      transformations {
        primitive_transformation {
          replace_with_info_type_config = true
        }
      }
    }
  }
}

# ---------------------------------------------------------------- Model Armor templates
resource "google_model_armor_template" "pm_prompt" {
  project     = var.project_id
  location    = var.region
  template_id = "ma-pm-prompt"
  labels      = var.labels

  filter_config {
    pi_and_jailbreak_filter_settings {
      filter_enforcement = "ENABLED"
      confidence_level   = var.pi_jailbreak_confidence
    }

    malicious_uri_filter_settings {
      filter_enforcement = "ENABLED"
    }

    rai_settings {
      rai_filters {
        filter_type      = "DANGEROUS"
        confidence_level = "MEDIUM_AND_ABOVE"
      }
      rai_filters {
        filter_type      = "HARASSMENT"
        confidence_level = "MEDIUM_AND_ABOVE"
      }
      rai_filters {
        filter_type      = "HATE_SPEECH"
        confidence_level = "MEDIUM_AND_ABOVE"
      }
      rai_filters {
        filter_type      = "SEXUALLY_EXPLICIT"
        confidence_level = "MEDIUM_AND_ABOVE"
      }
    }

    sdp_settings {
      basic_config {
        filter_enforcement = "ENABLED"
      }
    }
  }

  template_metadata {
    enforcement_type        = var.enforcement_type
    log_template_operations = true
    log_sanitize_operations = true
  }
}

resource "google_model_armor_template" "tool_response" {
  project     = var.project_id
  location    = var.region
  template_id = "ma-tool-response"
  labels      = var.labels

  filter_config {
    pi_and_jailbreak_filter_settings {
      filter_enforcement = "ENABLED"
      confidence_level   = var.pi_jailbreak_confidence
    }

    malicious_uri_filter_settings {
      filter_enforcement = "ENABLED"
    }

    sdp_settings {
      advanced_config {
        inspect_template    = google_data_loss_prevention_inspect_template.tool_payloads.id
        deidentify_template = google_data_loss_prevention_deidentify_template.tool_payloads.id
      }
    }
  }

  template_metadata {
    enforcement_type        = var.enforcement_type
    log_template_operations = true
    log_sanitize_operations = true
  }

  depends_on = [google_project_iam_member.modelarmor_dlp]
}

# Advanced SDP mode: the Model Armor service agent reads/uses the SDP templates.
resource "google_project_iam_member" "modelarmor_dlp" {
  for_each = toset(["roles/dlp.user", "roles/dlp.reader"])
  project  = var.project_id
  role     = each.value
  member   = "serviceAccount:${var.modelarmor_service_agent}"
}
