# dev — workspace default project (GEMINI.md). Placeholders are tolerated here: org-scoped resources (workforce pool,
# VPC-SC perimeter, PAB) are skipped and the IAP egress policies are rendered for review only.
env                 = "dev"
project_id          = "chayan-genai-bb"
region              = "us-central1"
strict_placeholders = false

# ---- SLB inputs (docs/15)
org_id                 = "PLACEHOLDER:Q-10:gcp_org_id"
vpcsc_access_policy_id = "PLACEHOLDER:Q-10:vpcsc_access_policy_id"
idp_issuer_uri         = "PLACEHOLDER:Q-08:entra_oidc_issuer_uri"
idp_client_id          = "PLACEHOLDER:Q-08:entra_wif_client_id"
event_gateway_hostname = "PLACEHOLDER:Q-10:event_gateway_hostname"
dr_region              = null

# ---- Agent Runtime phase switch (set true + real engine ids after the first SDK deploy; see infra/README.md)
agent_runtime_deployed = false
# reasoning_engine_ids = { pm_agent_core = "<id>", signal_ingestion = "<id>", commit_executor = "<id>" }

# ---- rollout: dev stays in dry-run (docs/12 §1)
access_policy_dry_run = true
policy_model          = "IAP_ALLOW_V1"
policy_apply_mode     = "native"
vpcsc_enforce         = false
enable_pab            = false

# ---- sizing / safety (dev sandbox)
kms_protection_level      = "HSM"
bucket_prefix             = "chayan-genai-bb-" # bucket names are global
lock_ledger_retention     = false              # NEVER lock in a shared sandbox: locking is irreversible (7-year WORM)
ledger_retention_days     = 30
alloydb_cpu_count         = 2
alloydb_availability_type = "ZONAL"
alloydb_read_pool_nodes   = 0
deletion_protection       = false
nat_static_ip_count       = 1

mcp_functional_type_enabled = false
notification_channel_ids    = []
cloudbuild_repository       = null
