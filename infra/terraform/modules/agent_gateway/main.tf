# Agent Gateway (docs/03, docs/08 §2/§5, docs/12 §3). Provider support CONFIRMED in hashicorp/google:
#   google_network_services_agent_gateway               (GA >= 7.37/7.38; beta since 7.21)
#   google_network_services_agent_connectivity_template (>= 8.4.0; vpc_egress ALL_TRAFFIC)
#   google_network_services_authz_extension + google_network_security_authz_policy (Model Armor CONTENT_AUTHZ)
# Source: docs.cloud.google.com/gemini-enterprise-agent-platform/govern/gateways/set-up-agent-gateway and
#         .../govern/gateways/delegate-authorization (Terraform tabs).
# The IAP REQUEST_AUTHZ extension that carries the DRY_RUN / ENFORCED switch lives in modules/access_policies
# (the dry-run flag is owned by the policy module, docs/12 §3).

locals {
  prefix          = "agw-${var.env}"
  dep_agent       = "serviceAccount:service-${var.project_number}@gcp-sa-dep.iam.gserviceaccount.com"
  re_agent        = "serviceAccount:service-${var.project_number}@gcp-sa-aiplatform-re.iam.gserviceaccount.com"
  modelarmor_host = "modelarmor.${var.region}.rep.googleapis.com"
}

# ---------------------------------------------------------------- egress connectivity (PSC-I into our VPC)
resource "google_network_services_agent_connectivity_template" "egress" {
  project                        = var.project_id
  location                       = var.region
  agent_connectivity_template_id = "${local.prefix}-egress-vpc"
  description                    = "Agent-to-Anywhere egress into the PM Agent VPC (ALL_TRAFFIC => VPC-SC enforceable)"
  access_path                    = "AGENT_TO_ANYWHERE"
  labels                         = var.labels

  egress_network_config {
    network_attachment = var.egress_network_attachment_id
    vpc_egress         = var.vpc_egress

    dynamic "dns_peering_config" {
      for_each = length(var.dns_peering_domains) > 0 && var.dns_peering_target_network != null ? [1] : []
      content {
        domains        = var.dns_peering_domains
        target_network = var.dns_peering_target_network
      }
    }

    dynamic "tls_config" {
      for_each = var.egress_trust_config == null ? [] : [var.egress_trust_config]
      content {
        trust_config     = tls_config.value
        additional_roots = "NO_ADDITIONAL_ROOTS"
      }
    }
  }
}

# ---------------------------------------------------------------- gateways
resource "google_network_services_agent_gateway" "egress" {
  project     = var.project_id
  location    = var.region
  name        = "${local.prefix}-egress"
  description = "Agent-to-Anywhere: every agent tool / A2A / model call (AID-CORE, AID-INGEST, AID-COMMIT)"
  labels      = var.labels

  google_managed {
    governed_access_path = "AGENT_TO_ANYWHERE"
  }

  registries                  = [var.registry_uri]
  agent_connectivity_template = "projects/${var.project_number}/locations/${var.region}/agentConnectivityTemplates/${google_network_services_agent_connectivity_template.egress.agent_connectivity_template_id}"
}

resource "google_network_services_agent_gateway" "ingress" {
  project     = var.project_id
  location    = var.region
  name        = "${local.prefix}-ingress"
  description = "Client-to-Agent: pm-workbench BFF (A2A/SSE) -> pm-agent-core; Cloud Scheduler -> signal-ingestion"
  labels      = var.labels

  google_managed {
    governed_access_path = "CLIENT_TO_AGENT"
  }
}

# ---------------------------------------------------------------- Model Armor service-agent permissions
resource "google_project_iam_member" "dep_model_armor" {
  for_each = var.model_armor_enabled ? toset(["roles/modelarmor.calloutUser", "roles/serviceusage.serviceUsageConsumer", "roles/modelarmor.user"]) : toset([])
  project  = var.project_id
  role     = each.value
  member   = local.dep_agent

  depends_on = [google_network_services_agent_gateway.egress, google_network_services_agent_gateway.ingress]
}

resource "google_project_iam_member" "re_model_armor" {
  for_each = var.model_armor_enabled && var.reasoning_engine_service_agent_exists ? toset(["roles/modelarmor.calloutUser", "roles/modelarmor.user"]) : toset([])
  project  = var.project_id
  role     = each.value
  member   = local.re_agent
}

# ---------------------------------------------------------------- Model Armor bindings (CONTENT_AUTHZ)
resource "google_network_services_authz_extension" "ma_egress" {
  count     = var.model_armor_enabled ? 1 : 0
  project   = var.project_id
  location  = var.region
  name      = "${local.prefix}-ma-egress"
  service   = local.modelarmor_host
  timeout   = "1s"
  fail_open = false
  metadata = {
    model_armor_settings = jsonencode([{
      request_template_id  = var.egress_model_armor_template
      response_template_id = var.egress_model_armor_template
    }])
  }

  depends_on = [google_project_iam_member.dep_model_armor]
}

resource "google_network_security_authz_policy" "ma_egress" {
  count          = var.model_armor_enabled ? 1 : 0
  project        = var.project_id
  location       = var.region
  name           = "${local.prefix}-ma-egress"
  description    = "Model Armor ma-tool-response on MCP / A2A / LLM payloads (docs/08 §5)"
  policy_profile = "CONTENT_AUTHZ"
  action         = "CUSTOM"

  target {
    resources = [google_network_services_agent_gateway.egress.id]
  }

  custom_provider {
    authz_extension {
      resources = [google_network_services_authz_extension.ma_egress[0].id]
    }
  }

  http_rules {
    to {
      operations {
        paths {
          prefix = "/"
        }
      }
    }
    when = "request.headers['content-type'] == 'application/json' || request.headers['content-type'].startsWith('text/')"
  }
}

# Ingress supports a single CONTENT_AUTHZ policy: Model Armor here means Semantic Governance cannot be attached
# to the ingress gateway (documented limitation; docs/08 §5).
resource "google_network_services_authz_extension" "ma_ingress" {
  count     = var.model_armor_enabled ? 1 : 0
  project   = var.project_id
  location  = var.region
  name      = "${local.prefix}-ma-ingress"
  service   = local.modelarmor_host
  timeout   = "1s"
  fail_open = false
  metadata = {
    model_armor_settings = jsonencode([{
      request_template_id  = var.ingress_model_armor_template
      response_template_id = var.ingress_model_armor_template
    }])
  }

  depends_on = [google_project_iam_member.dep_model_armor]
}

resource "google_network_security_authz_policy" "ma_ingress" {
  count          = var.model_armor_enabled ? 1 : 0
  project        = var.project_id
  location       = var.region
  name           = "${local.prefix}-ma-ingress"
  description    = "Model Armor ma-pm-prompt on Workbench prompts and agent responses (docs/08 §5)"
  policy_profile = "CONTENT_AUTHZ"
  action         = "CUSTOM"

  target {
    resources = [google_network_services_agent_gateway.ingress.id]
  }

  custom_provider {
    authz_extension {
      resources = [google_network_services_authz_extension.ma_ingress[0].id]
    }
  }
}
