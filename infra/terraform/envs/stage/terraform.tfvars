# stage — SLB sandbox connectors, UAT, online evals (docs/12 §1). Strict: every placeholder must be resolved
# before `terraform plan` succeeds (project_id is validated first).
env                 = "stage"
project_id          = "PLACEHOLDER:Q-10:stage_project_id"
region              = "us-central1"
strict_placeholders = true

org_id                 = "PLACEHOLDER:Q-10:gcp_org_id"
vpcsc_access_policy_id = "PLACEHOLDER:Q-10:vpcsc_access_policy_id"
idp_issuer_uri         = "PLACEHOLDER:Q-08:entra_oidc_issuer_uri"
idp_client_id          = "PLACEHOLDER:Q-08:entra_wif_client_id"
event_gateway_hostname = "PLACEHOLDER:Q-10:event_gateway_hostname"
dr_region              = null

agent_runtime_deployed = false

# Dry-run first; flip to false (Enforce, docs/12 §1) after zero unexpected denials over the soak window.
access_policy_dry_run = true
policy_model          = "IAP_ALLOW_V1"
vpcsc_enforce         = false

lock_ledger_retention     = true
ledger_retention_days     = 2557
alloydb_cpu_count         = 4
alloydb_availability_type = "REGIONAL"
alloydb_read_pool_nodes   = 0
deletion_protection       = true
nat_static_ip_count       = 2
