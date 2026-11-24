output "project_number" {
  value = local.pnum
}

output "unresolved_placeholders" {
  description = "SLB inputs still pending (docs/15). Must be empty before stage/prod."
  value       = local.unresolved
}

# ---------------------------------------------------------------- Agent Runtime deploy inputs (deploy/agent_runtime)
output "agent_runtime_deploy_config" {
  description = "Values for client.runtimes.create(config=...) — identity_type and agent_gateway_config are immutable at first deploy."
  value = {
    identity_type = "AGENT_IDENTITY"
    agent_gateway_config = {
      agent_to_anywhere_config = { agent_gateway = module.agent_gateway.egress_gateway_id }
      client_to_agent_config   = { agent_gateway = module.agent_gateway.ingress_gateway_id }
    }
    psc_interface_config = { network_attachment = module.network.agent_runtime_network_attachment_id }
    encryption_spec      = { kms_key_name = module.kms.cmek_key_ids["agent-runtime"] }
    artifact_registry    = module.ci.artifact_registry_repository
  }
}

output "agent_principals" {
  description = "Expected Agent Identity principals (AID-CORE / AID-INGEST / AID-COMMIT)."
  value       = local.agent_principals
}

output "egress_gateway_root_certificates" {
  description = "Bake into BYOC Agent Runtime images (container-image deploys) so TLS inspection by the gateway is trusted."
  value       = module.agent_gateway.egress_gateway_root_certificates
}

# ---------------------------------------------------------------- policies
output "access_policy_enforcement_mode" {
  value = module.access_policies.enforcement_mode
}

output "access_policies_applied" {
  value = module.access_policies.applied
}

output "access_policy_pairs" {
  value = module.access_policies.pairs
}

output "rendered_policy_dir" {
  value = module.access_policies.rendered_dir
}

output "vpcsc_perimeter" {
  value = { name = module.perimeter.perimeter_name, mode = module.perimeter.mode, skipped = module.perimeter.skipped_reason }
}

# ---------------------------------------------------------------- services, registry, data
output "cloud_run_uris" {
  value = { for k, m in module.cloud_run : k => m.uri }
}

output "service_accounts" {
  value = merge(module.identity.cloud_run_sa_emails, module.identity.gke_sa_emails)
}

output "mcp_server_ids" {
  value = module.agent_registry.mcp_server_ids
}

output "endpoint_ids" {
  value = module.agent_registry.endpoint_ids
}

output "nat_egress_ips" {
  description = "Give to SLB for ADO / ServiceNow conditional-access allow-lists."
  value       = module.network.nat_egress_ips
}

output "kms" {
  value = {
    PM_AGENT_APPROVAL_KEY = module.kms.approval_token_signer_version
    PM_AGENT_VERIFIER_KEY = module.kms.verifier_report_signer_version
    PM_AGENT_SDP_KMS_KEY  = module.kms.sdp_wrap_key_id
  }
}

output "data" {
  value = {
    alloydb_instance      = module.data.alloydb_primary_instance_name
    alloydb_host          = module.data.alloydb_hostname
    artifacts_bucket      = module.data.artifacts_bucket
    ledger_exports_bucket = module.data.ledger_exports_bucket
    analytics_dataset     = module.data.analytics_dataset_id
    ledger_log_bucket     = module.logging.bucket_name
    ledger_linked_dataset = module.logging.linked_bigquery_dataset
    topics                = module.data.topic_ids
    secrets               = module.data.secret_ids
  }
}

# ---------------------------------------------------------------- deploy/k8s overlay values
output "k8s_overlay_values" {
  description = "Substitute into deploy/k8s/overlays/<env>."
  value = {
    project_id                    = var.project_id
    cluster                       = module.gke.cluster_name
    gsa                           = module.identity.gke_sa_emails
    ledger_anchor_gsa             = trimprefix(local.run_sa["mcp-ledger"], "serviceAccount:")
    event_gateway_static_ip_name  = module.gke.event_gateway_static_ip_name
    event_gateway_security_policy = module.gke.event_gateway_security_policy
    event_gateway_hostname        = var.event_gateway_hostname
    correlation_ilb_address_name  = module.network.correlation_ilb_address_name
    correlation_mcp_hostname      = module.network.correlation_mcp_hostname
    image_repository              = module.ci.artifact_registry_repository
    webhook_secret_env            = { for sid, w in local.webhook_secrets : w.env => sid } # ExternalSecret data map
  }
}

output "ci_service_account_email" {
  value = module.ci.ci_service_account_email
}

output "secret_accessors" {
  description = "Secret Manager secret -> workloads allowed to read it (owning workload only)."
  value       = local.secrets
}

output "service_env_contract" {
  description = "Per Cloud Run service: env var NAMES (plain) and env vars projected from Secret Manager — values omitted."
  value = {
    for s in keys(local.run_services) : s => {
      env        = sort(keys(local.service_env[s]))
      secret_env = sort(keys(lookup(local.secret_env_bindings, s, {})))
    }
  }
}
