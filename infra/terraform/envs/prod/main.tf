# Thin root: every environment instantiates the same composition (modules/platform); only terraform.tfvars and
# backend.hcl differ between envs/dev, envs/stage and envs/prod (keep main.tf / variables.tf / outputs.tf identical).

module "platform" {
  source = "../../modules/platform"

  env        = var.env
  project_id = var.project_id
  region     = var.region

  org_id                 = var.org_id
  vpcsc_access_policy_id = var.vpcsc_access_policy_id
  idp_issuer_uri         = var.idp_issuer_uri
  idp_client_id          = var.idp_client_id
  event_gateway_hostname = var.event_gateway_hostname
  dr_region              = var.dr_region

  agent_identity_trust_domain = var.agent_identity_trust_domain
  reasoning_engine_ids        = var.reasoning_engine_ids
  agent_runtime_deployed      = var.agent_runtime_deployed

  access_policy_dry_run = var.access_policy_dry_run
  policy_model          = var.policy_model
  policy_apply_mode     = var.policy_apply_mode
  vpcsc_enforce         = var.vpcsc_enforce
  enable_pab            = var.enable_pab

  kms_protection_level        = var.kms_protection_level
  bucket_prefix               = var.bucket_prefix
  lock_ledger_retention       = var.lock_ledger_retention
  ledger_retention_days       = var.ledger_retention_days
  alloydb_cpu_count           = var.alloydb_cpu_count
  alloydb_availability_type   = var.alloydb_availability_type
  alloydb_read_pool_nodes     = var.alloydb_read_pool_nodes
  deletion_protection         = var.deletion_protection
  nat_static_ip_count         = var.nat_static_ip_count
  gke_master_authorized_cidrs = var.gke_master_authorized_cidrs
  event_gateway_allowed_cidrs = var.event_gateway_allowed_cidrs
  mcp_functional_type_enabled = var.mcp_functional_type_enabled
  notification_channel_ids    = var.notification_channel_ids
  cloudbuild_repository       = var.cloudbuild_repository
  extra_apis                  = var.extra_apis
}
