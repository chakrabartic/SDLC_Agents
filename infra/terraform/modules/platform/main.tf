# PM Agent platform for one environment: wires every module in infra/terraform/modules (docs/12 §3).
# Apply order and the dry-run -> enforce procedure are in infra/README.md.

data "google_project" "this" {
  project_id = var.project_id
}

locals {
  pnum   = data.google_project.this.number
  labels = merge(var.labels, { app = "pm-agent", env = var.env, managed_by = "terraform" })

  # ------------------------------------------------------------ config/pm_agent.yaml (single source of truth)
  config_path       = coalesce(var.config_file, "${path.root}/../../../../config/pm_agent.yaml")
  app_config        = try(yamldecode(file(local.config_path)), {})
  custom_info_types = try(local.app_config.grounding.custom_infotypes, {})
  idp_groups        = try(local.app_config.hitl.idp_group_mapping, {})

  # ------------------------------------------------------------ placeholder handling
  slb_inputs = merge(
    {
      org_id                 = var.org_id
      vpcsc_access_policy_id = var.vpcsc_access_policy_id
      idp_issuer_uri         = var.idp_issuer_uri
      idp_client_id          = var.idp_client_id
      event_gateway_hostname = var.event_gateway_hostname
      dr_region              = coalesce(var.dr_region, "none")
    },
    { for k, v in local.idp_groups : "idp_group.${k}" => tostring(v) },
  )
  unresolved = sort([for k, v in local.slb_inputs : "${k}=${v}" if startswith(v, "PLACEHOLDER:")])

  org_known = !startswith(var.org_id, "PLACEHOLDER:")

  # ------------------------------------------------------------ Agent Identity principals (docs/08 §3.1)
  trust_domain  = coalesce(var.agent_identity_trust_domain, "agents.global.org-${var.org_id}.system.id.goog")
  engine_prefix = "principal://${local.trust_domain}/resources/aiplatform/projects/${local.pnum}/locations/${var.region}/reasoningEngines"
  agent_principals = {
    core   = "${local.engine_prefix}/${var.reasoning_engine_ids.pm_agent_core}"
    ingest = "${local.engine_prefix}/${var.reasoning_engine_ids.signal_ingestion}"
    commit = "${local.engine_prefix}/${var.reasoning_engine_ids.commit_executor}"
  }
  agents_live   = var.agent_runtime_deployed && alltrue([for p in values(local.agent_principals) : !strcontains(p, "PLACEHOLDER:")])
  agent_members = local.agents_live ? values(local.agent_principals) : []

  # ------------------------------------------------------------ Google service agents (deterministic emails)
  sa = {
    alloydb          = "service-${local.pnum}@gcp-sa-alloydb.iam.gserviceaccount.com"
    pubsub           = "service-${local.pnum}@gcp-sa-pubsub.iam.gserviceaccount.com"
    secretmanager    = "service-${local.pnum}@gcp-sa-secretmanager.iam.gserviceaccount.com"
    artifactregistry = "service-${local.pnum}@gcp-sa-artifactregistry.iam.gserviceaccount.com"
    aiplatform       = "service-${local.pnum}@gcp-sa-aiplatform.iam.gserviceaccount.com"
    reasoning_engine = "service-${local.pnum}@gcp-sa-aiplatform-re.iam.gserviceaccount.com"
    modelarmor       = "service-${local.pnum}@gcp-sa-modelarmor.iam.gserviceaccount.com"
    iap              = "service-${local.pnum}@gcp-sa-iap.iam.gserviceaccount.com"
    dlp              = "service-${local.pnum}@dlp-api.iam.gserviceaccount.com"
    gke              = "service-${local.pnum}@container-engine-robot.iam.gserviceaccount.com"
  }

  apis = toset(concat([
    "accesscontextmanager.googleapis.com", "agentregistry.googleapis.com", "aiplatform.googleapis.com",
    "alloydb.googleapis.com", "artifactregistry.googleapis.com", "bigquery.googleapis.com",
    "binaryauthorization.googleapis.com", "cloudbuild.googleapis.com", "cloudkms.googleapis.com",
    "cloudresourcemanager.googleapis.com", "cloudtrace.googleapis.com", "compute.googleapis.com",
    "container.googleapis.com", "containeranalysis.googleapis.com", "dlp.googleapis.com", "dns.googleapis.com",
    "iam.googleapis.com", "iamcredentials.googleapis.com", "iap.googleapis.com", "logging.googleapis.com",
    "modelarmor.googleapis.com", "monitoring.googleapis.com", "networksecurity.googleapis.com",
    "networkservices.googleapis.com", "orgpolicy.googleapis.com", "pubsub.googleapis.com", "run.googleapis.com",
    "secretmanager.googleapis.com", "serviceusage.googleapis.com", "storage.googleapis.com",
  ], var.extra_apis))

  # ------------------------------------------------------------ deployment units (docs/12 §2)
  read_connectors  = ["mcp-ado", "mcp-servicenow", "mcp-pagerduty", "mcp-msgraph", "mcp-kb", "mcp-figma"]
  write_connectors = ["mcp-ado-write", "mcp-kb-write", "mcp-figma-write", "mcp-msgraph-write"]

  svc_defaults = { min = 0, max = 20, cpu = "1", memory = "1Gi", cpu_idle = true, affinity = false, ingress = "INGRESS_TRAFFIC_INTERNAL_ONLY", egress = "PRIVATE_RANGES_ONLY", mcp = true }

  run_services = merge(
    # SaaS connectors egress through Cloud NAT static IPs (ALL_TRAFFIC) so SLB allow-lists can pin them.
    { for s in local.read_connectors : s => merge(local.svc_defaults, { egress = "ALL_TRAFFIC" }) },
    { for s in local.write_connectors : s => merge(local.svc_defaults, { egress = "ALL_TRAFFIC" }) },
    {
      "mcp-store"              = merge(local.svc_defaults, { min = 1, max = 50, memory = "2Gi" })
      "mcp-verifier"           = merge(local.svc_defaults, { min = 1, max = 50, cpu = "2", memory = "2Gi", cpu_idle = false })
      "mcp-ledger"             = merge(local.svc_defaults, { min = 1, max = 20 })
      "svc-prototype-renderer" = merge(local.svc_defaults, { max = 5, ingress = "INGRESS_TRAFFIC_ALL" })
      "pm-workbench"           = merge(local.svc_defaults, { min = 1, max = 10, affinity = true, ingress = "INGRESS_TRAFFIC_ALL", mcp = false })
    },
  )

  gke_workloads = {
    "signal-event-gateway" = { namespace = "pm-agent", ksa = "signal-event-gateway" }
    "correlation-engine"   = { namespace = "pm-agent", ksa = "correlation-engine" }
    "dls-catalog-sync"     = { namespace = "pm-agent", ksa = "dls-catalog-sync" }
  }

  # Third-party credentials / webhook secrets: values added out-of-band; readable only by the owning workload.
  secrets = {
    "ado-read-credential"        = ["mcp-ado"]
    "ado-write-credential"       = ["mcp-ado-write"]
    "servicenow-read-credential" = ["mcp-servicenow"]
    "pagerduty-read-credential"  = ["mcp-pagerduty"]
    "msgraph-read-credential"    = ["mcp-msgraph"]
    "msgraph-send-credential"    = ["mcp-msgraph-write"]
    "kb-read-credential"         = ["mcp-kb"]
    "kb-publish-credential"      = ["mcp-kb-write"]
    "figma-plan-access-token"    = ["mcp-figma", "dls-catalog-sync"]
    "figma-comments-credential"  = ["mcp-figma-write"]
    "renderer-plugin-token-key"  = ["svc-prototype-renderer"]
    "webhook-ado-secret"         = ["signal-event-gateway"]
    "webhook-servicenow-secret"  = ["signal-event-gateway"]
    "webhook-pagerduty-secret"   = ["signal-event-gateway"]
    "webhook-graph-client-state" = ["signal-event-gateway"]
  }

  # Agent Gateway needs these Google APIs registered + allowed once agents are bound to an ENFORCING gateway
  # (exact host matching; docs: scale/runtime/agent-gateway-runtime-deploy "Allowlist essential APIs").
  essential_hosts = [
    "agentregistry.googleapis.com",
    "logging.googleapis.com", "logging.mtls.googleapis.com",
    "telemetry.googleapis.com", "telemetry.mtls.googleapis.com",
    "cloudtrace.googleapis.com", "cloudtrace.mtls.googleapis.com",
    "monitoring.googleapis.com", "monitoring.mtls.googleapis.com",
    "secretmanager.googleapis.com", "secretmanager.mtls.googleapis.com",
    "cloudresourcemanager.googleapis.com", "cloudresourcemanager.mtls.googleapis.com",
    "iamcredentials.googleapis.com", "iamcredentials.mtls.googleapis.com",
    "aiplatform.googleapis.com", "aiplatform.mtls.googleapis.com",
    "${var.region}-aiplatform.googleapis.com", "${var.region}-aiplatform.mtls.googleapis.com",
    "aiplatform.${var.region}.rep.googleapis.com",
  ]
}

# ================================================================= APIs + Google service agents
resource "google_project_service" "apis" {
  for_each           = local.apis
  project            = var.project_id
  service            = each.value
  disable_on_destroy = false
}

resource "google_project_service_identity" "agents" {
  provider = google-beta
  for_each = toset([
    "alloydb.googleapis.com", "pubsub.googleapis.com", "secretmanager.googleapis.com", "artifactregistry.googleapis.com",
    "aiplatform.googleapis.com", "iap.googleapis.com", "modelarmor.googleapis.com", "dlp.googleapis.com",
  ])
  project = var.project_id
  service = each.value

  depends_on = [google_project_service.apis]
}

data "google_storage_project_service_account" "gcs" {
  project    = var.project_id
  depends_on = [google_project_service.apis]
}

data "google_bigquery_default_service_account" "bq" {
  project    = var.project_id
  depends_on = [google_project_service.apis]
}

data "google_logging_project_cmek_settings" "logging" {
  project    = var.project_id
  depends_on = [google_project_service.apis]
}

# ================================================================= network / KMS / identities
module "network" {
  source              = "../network"
  project_id          = var.project_id
  region              = var.region
  env                 = var.env
  nat_static_ip_count = var.nat_static_ip_count
  labels              = local.labels

  depends_on = [google_project_service.apis]
}

module "identity" {
  source             = "../identity"
  project_id         = var.project_id
  region             = var.region
  env                = var.env
  org_id             = var.org_id
  cloud_run_services = toset(keys(local.run_services))
  gke_workloads      = local.gke_workloads
  idp_issuer_uri     = var.idp_issuer_uri
  idp_client_id      = var.idp_client_id
  labels             = local.labels

  depends_on = [google_project_service.apis]
}

locals {
  run_sa  = { for s in keys(local.run_services) : s => "serviceAccount:${substr("sa-${s}", 0, 30)}@${var.project_id}.iam.gserviceaccount.com" }
  gke_sa  = { for k in keys(local.gke_workloads) : k => "serviceAccount:${substr("sa-gke-${k}", 0, 30)}@${var.project_id}.iam.gserviceaccount.com" }
  all_sas = concat(values(local.run_sa), values(local.gke_sa))
  wl_sa   = merge(local.run_sa, local.gke_sa)
}

module "kms" {
  source           = "../kms"
  project_id       = var.project_id
  region           = var.region
  env              = var.env
  protection_level = var.kms_protection_level
  labels           = local.labels

  cmek_encrypter_decrypters = {
    alloydb          = { alloydb = "serviceAccount:${local.sa.alloydb}" }
    gcs              = { gcs = "serviceAccount:${data.google_storage_project_service_account.gcs.email_address}" }
    bigquery         = { bigquery = "serviceAccount:${data.google_bigquery_default_service_account.bq.email}" }
    pubsub           = { pubsub = "serviceAccount:${local.sa.pubsub}" }
    logging          = { logging = "serviceAccount:${data.google_logging_project_cmek_settings.logging.service_account_id}" }
    secretmanager    = { secretmanager = "serviceAccount:${local.sa.secretmanager}" }
    artifactregistry = { artifactregistry = "serviceAccount:${local.sa.artifactregistry}" }
    gke              = { gke = "serviceAccount:${local.sa.gke}" }
    agent-runtime = merge(
      { aiplatform = "serviceAccount:${local.sa.aiplatform}" },
      var.agent_runtime_deployed ? { reasoning_engine = "serviceAccount:${local.sa.reasoning_engine}" } : {},
    )
  }

  # Signers: exactly one SA per key (docs/07 §6, docs/10 §7). Every component may verify (lead alignment).
  approval_token_signers  = [local.run_sa["mcp-ledger"]]
  verifier_report_signers = [local.run_sa["mcp-verifier"]]
  signing_key_viewers     = concat(local.all_sas, local.agent_members)
  sdp_wrap_decrypters     = ["serviceAccount:${local.sa.dlp}", local.run_sa["mcp-verifier"], local.run_sa["mcp-store"]]

  depends_on = [google_project_service_identity.agents, google_project_service.apis, module.identity]
}

# ================================================================= data (AlloyDB, GCS, BigQuery, Pub/Sub, secrets) + ledger log bucket
module "data" {
  source                = "../data"
  project_id            = var.project_id
  project_number        = local.pnum
  region                = var.region
  env                   = var.env
  labels                = local.labels
  network_id            = module.network.network_id
  workloads_subnet_id   = module.network.workloads_subnet_id
  private_dns_zone_name = module.network.private_dns_zone_name
  private_dns_domain    = module.network.private_dns_domain

  kms_key_alloydb  = module.kms.cmek_key_ids["alloydb"]
  kms_key_gcs      = module.kms.cmek_key_ids["gcs"]
  kms_key_bigquery = module.kms.cmek_key_ids["bigquery"]
  kms_key_pubsub   = module.kms.cmek_key_ids["pubsub"]

  alloydb_cpu_count           = var.alloydb_cpu_count
  alloydb_availability_type   = var.alloydb_availability_type
  alloydb_read_pool_nodes     = var.alloydb_read_pool_nodes
  alloydb_deletion_protection = var.deletion_protection
  dr_region                   = var.dr_region
  dr_kms_key_alloydb          = null # needs a DR-region key ring (follow-up once Q-09 is answered)

  bucket_prefix                = var.bucket_prefix
  ledger_export_retention_days = var.ledger_retention_days
  lock_ledger_export_retention = var.lock_ledger_retention

  artifact_bucket_object_admins   = [local.run_sa["mcp-store"]]
  artifact_bucket_object_creators = concat([for s in local.read_connectors : local.run_sa[s]], [local.run_sa["svc-prototype-renderer"]])
  artifact_bucket_object_viewers  = [local.run_sa["pm-workbench"]]
  ledger_export_object_creators   = [local.run_sa["mcp-ledger"]]
  bigquery_data_editors           = concat([local.run_sa["mcp-store"]], local.agent_members) # BigQueryAgentAnalyticsPlugin runs in the agents
  bigquery_data_viewers           = [local.run_sa["pm-workbench"]]
  alloydb_clients = [
    local.run_sa["mcp-store"], local.run_sa["mcp-ledger"], local.run_sa["mcp-verifier"],
    local.gke_sa["correlation-engine"], local.gke_sa["dls-catalog-sync"],
  ]

  topic_publishers = {
    "signal-change-hints" = [local.gke_sa["signal-event-gateway"]] # hints only (T8)
    "signal-ingested"     = [local.run_sa["mcp-store"]]
    "problem-updated"     = [local.run_sa["mcp-store"]]
    "ledger-events"       = [local.run_sa["mcp-ledger"]]
  }
  subscription_subscribers = {
    "signal-change-hints-ingestion" = local.agents_live ? [local.agent_principals.ingest] : []
    "signal-ingested-correlation"   = [local.gke_sa["correlation-engine"]]
    "problem-updated-workbench"     = [local.run_sa["pm-workbench"]]
    "ledger-events-workbench"       = [local.run_sa["pm-workbench"]]
  }
  pubsub_service_agent = local.sa.pubsub

  secrets               = { for sid, owners in local.secrets : sid => [for wl in owners : local.wl_sa[wl]] }
  kms_key_secretmanager = module.kms.cmek_key_ids["secretmanager"]

  depends_on = [module.kms, module.identity]
}

module "logging" {
  source         = "../logging"
  project_id     = var.project_id
  region         = var.region
  retention_days = var.ledger_retention_days
  locked         = var.lock_ledger_retention
  kms_key_name   = module.kms.cmek_key_ids["logging"]

  depends_on = [module.kms]
}

# ================================================================= Model Armor
module "model_armor" {
  source                   = "../model_armor"
  project_id               = var.project_id
  region                   = var.region
  labels                   = local.labels
  custom_info_types        = local.custom_info_types
  modelarmor_service_agent = local.sa.modelarmor

  depends_on = [google_project_service_identity.agents]
}

# ================================================================= Cloud Run services (one SA each)
locals {
  common_env = {
    PM_AGENT_ENV                = var.env
    GOOGLE_CLOUD_PROJECT        = var.project_id
    GOOGLE_CLOUD_LOCATION       = var.region
    GOOGLE_GENAI_USE_VERTEXAI   = "true"
    GOOGLE_GENAI_USE_ENTERPRISE = "true"
    PM_AGENT_VERIFIER_KEY       = module.kms.verifier_report_signer_version
    PM_AGENT_APPROVAL_KEY       = module.kms.approval_token_signer_version
  }

  secret_names_by_workload = {
    for wl in keys(local.run_services) : wl => join(",", [
      for sid, owners in local.secrets : "projects/${var.project_id}/secrets/${sid}" if contains(owners, wl)
    ])
  }

  db_env = {
    PM_AGENT_ALLOYDB_INSTANCE = module.data.alloydb_primary_instance_name
    PM_AGENT_ALLOYDB_HOST     = module.data.alloydb_hostname
  }

  service_env = {
    for s, cfg in local.run_services : s => merge(
      local.common_env,
      { PM_AGENT_COMPONENT = s },
      local.secret_names_by_workload[s] == "" ? {} : { PM_AGENT_CREDENTIAL_SECRETS = local.secret_names_by_workload[s] },
      contains(["mcp-store", "mcp-ledger", "mcp-verifier"], s) ? local.db_env : {},
      contains(["mcp-store", "pm-workbench", "svc-prototype-renderer"], s) || contains(local.read_connectors, s) ? { PM_AGENT_ARTIFACTS_BUCKET = module.data.artifacts_bucket } : {},
      s == "mcp-ledger" ? { PM_AGENT_LEDGER_LOG = module.logging.ledger_log_name, PM_AGENT_LEDGER_EXPORTS_BUCKET = module.data.ledger_exports_bucket } : {},
    )
  }

  workforce_groups = local.org_known ? [
    for k, g in local.idp_groups : "${module.identity.workforce_principal_set_prefix}/group/${g}" if !startswith(tostring(g), "PLACEHOLDER:")
  ] : []

  # Who may pass IAP on each service. MCP servers: the calling agent identities (end-to-end via the gateway —
  # exact principal IAP sees for gateway traffic is UNCONFIRMED, verify in the dry-run soak). The BFF reaches
  # mcp-ledger (decide), mcp-store (re-identify for display) and mcp-verifier (redact PM input) directly.
  iap_accessors = {
    for s, cfg in local.run_services : s => distinct(concat(
      cfg.mcp ? local.agent_members : [],
      contains(["mcp-ledger", "mcp-store", "mcp-verifier"], s) ? [local.run_sa["pm-workbench"]] : [],
      contains(["pm-workbench", "svc-prototype-renderer"], s) ? local.workforce_groups : [],
    ))
  }
}

module "cloud_run" {
  source   = "../cloud_run_service"
  for_each = local.run_services

  project_id            = var.project_id
  region                = var.region
  name                  = each.key
  service_account_email = trimprefix(local.run_sa[each.key], "serviceAccount:")
  ingress               = each.value.ingress
  iap_enabled           = true
  functional_type       = each.value.mcp && var.mcp_functional_type_enabled ? "FUNCTIONAL_TYPE_MCP_SERVER" : null
  invoker_members       = ["serviceAccount:${local.sa.iap}"] # ONLY the IAP service agent (docs/08 §4)
  iap_accessors         = local.iap_accessors[each.key]
  min_instances         = each.value.min
  max_instances         = each.value.max
  cpu                   = each.value.cpu
  memory                = each.value.memory
  cpu_idle              = each.value.cpu_idle
  session_affinity      = each.value.affinity
  env_vars              = local.service_env[each.key]
  vpc_subnetwork        = module.network.workloads_subnet_id
  vpc_egress            = each.value.egress
  deletion_protection   = var.deletion_protection
  labels                = local.labels

  depends_on = [module.identity, google_project_service_identity.agents]
}

# ================================================================= Agent Registry + Agent Gateway + policies (same apply)
module "agent_registry" {
  source     = "../agent_registry"
  project_id = var.project_id
  region     = var.region

  mcp_servers = merge(
    { for s, cfg in local.run_services : s => { url = "${module.cloud_run[s].uri}/mcp", description = "PM Agent ${s} (Cloud Run, IAP)" } if cfg.mcp },
    { "mcp-correlation" = { url = "https://${module.network.correlation_mcp_hostname}/mcp", description = "correlation-engine (GKE internal LB)" } },
  )

  endpoints = merge(
    { for h in local.essential_hosts : replace(h, ".", "-") => { url = "https://${h}", description = "Essential platform API for Agent Runtime behind Agent Gateway" } },
    var.agent_runtime_deployed ? {
      "commit-executor" = {
        url         = "https://${var.region}-aiplatform.mtls.googleapis.com/v1/projects/${local.pnum}/locations/${var.region}/reasoningEngines/${var.reasoning_engine_ids.commit_executor}"
        description = "commit-executor (Agent Runtime) — A2A target of pm-agent-core only"
      }
    } : {},
  )

  depends_on = [google_project_service.apis]
}

module "agent_gateway" {
  source                       = "../agent_gateway"
  project_id                   = var.project_id
  project_number               = local.pnum
  region                       = var.region
  env                          = var.env
  labels                       = local.labels
  registry_uri                 = module.agent_registry.registry_uri
  egress_network_attachment_id = module.network.agent_gateway_network_attachment_id
  dns_peering_domains          = [module.network.private_dns_domain]
  dns_peering_target_network   = "projects/${var.project_id}/global/networks/${module.network.network_name}"
  ingress_model_armor_template = module.model_armor.pm_prompt_template_name
  egress_model_armor_template  = module.model_armor.tool_response_template_name

  reasoning_engine_service_agent_exists = var.agent_runtime_deployed

  depends_on = [google_project_service.apis]
}

module "access_policies" {
  source            = "../access_policies"
  project_id        = var.project_id
  region            = var.region
  env               = var.env
  org_id            = var.org_id
  dry_run           = var.access_policy_dry_run
  policy_model      = var.policy_model
  apply_mode        = var.policy_apply_mode
  apply_enabled     = var.agent_runtime_deployed
  enable_pab        = var.enable_pab
  egress_gateway_id = module.agent_gateway.egress_gateway_id
  agent_principals  = local.agent_principals

  mcp_server_ids       = module.agent_registry.mcp_server_ids
  mcp_server_resources = module.agent_registry.mcp_server_resources
  tools_by_server      = module.agent_registry.tools_by_server
  endpoint_ids         = module.agent_registry.endpoint_ids
  endpoint_resources   = module.agent_registry.endpoint_resources
}

# ================================================================= GKE data plane
module "gke" {
  source                  = "../gke"
  project_id              = var.project_id
  region                  = var.region
  env                     = var.env
  labels                  = local.labels
  network_id              = module.network.network_id
  subnetwork_id           = module.network.workloads_subnet_id
  pods_range_name         = module.network.gke_pods_range_name
  services_range_name     = module.network.gke_services_range_name
  master_authorized_cidrs = var.gke_master_authorized_cidrs
  database_encryption_key = module.kms.cmek_key_ids["gke"]
  deletion_protection     = var.deletion_protection

  workload_identity_bindings = merge(
    { for k, w in local.gke_workloads : trimprefix(local.gke_sa[k], "serviceAccount:") => w },
    # ledger-anchor CronJob runs AS the ledger identity (only SA-LEDGER may sign; docs/07 §8.1 anchors/exports).
    { (trimprefix(local.run_sa["mcp-ledger"], "serviceAccount:")) = { namespace = "pm-agent", ksa = "ledger-anchor" } },
  )
  event_gateway_allowed_cidrs = var.event_gateway_allowed_cidrs

  depends_on = [module.identity, module.kms]
}

# ================================================================= perimeter / observability / CI
module "perimeter" {
  source           = "../perimeter"
  env              = var.env
  access_policy_id = var.vpcsc_access_policy_id
  project_numbers  = [local.pnum]
  enforce          = var.vpcsc_enforce
}

module "observability" {
  source                   = "../observability"
  project_id               = var.project_id
  env                      = var.env
  ledger_log_name          = module.logging.ledger_log_name
  notification_channel_ids = var.notification_channel_ids

  depends_on = [google_project_service.apis]
}

module "ci" {
  source                         = "../ci"
  project_id                     = var.project_id
  region                         = var.region
  env                            = var.env
  labels                         = local.labels
  kms_key_name                   = module.kms.cmek_key_ids["artifactregistry"]
  runtime_service_account_emails = [for m in local.all_sas : trimprefix(m, "serviceAccount:")]
  cloudbuild_repository          = var.cloudbuild_repository

  depends_on = [module.identity, module.kms]
}

# ================================================================= commit-executor invocation (defense in depth)
# Only AID-CORE may invoke commit-executor (A2A `execute`, docs/08 §3.2): authoritative engine-level IAM, in addition
# to the gateway egress policy on the commit-executor registry endpoint.
resource "google_vertex_ai_reasoning_engine_iam_binding" "executor_invokers" {
  count            = local.agents_live ? 1 : 0
  project          = var.project_id
  region           = var.region
  reasoning_engine = var.reasoning_engine_ids.commit_executor
  role             = "roles/aiplatform.user"
  members          = [local.agent_principals.core]
}
