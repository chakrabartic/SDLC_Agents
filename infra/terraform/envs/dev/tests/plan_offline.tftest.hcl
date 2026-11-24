# Offline plan of the full composition with mocked Google providers — no credentials, no API calls.
#   cd infra/terraform/envs/dev && terraform init -backend=false && terraform test
# Exercises the phase switches (docs: infra/README.md §3), the authorization matrix and the strict-placeholder guard.

mock_provider "google" {
  # google_iam_policy is computed locally by the real provider; mocks would emit random strings, so return a valid
  # empty policy (policy CONTENT is reviewed via the rendered per-pair / merged JSON files).
  mock_data "google_iam_policy" {
    defaults = {
      policy_data = "{\"bindings\":[]}"
    }
  }

  override_data {
    target = module.platform.data.google_project.this
    values = { number = "123456789012" }
  }

  override_data {
    target = module.platform.data.google_storage_project_service_account.gcs
    values = { email_address = "service-123456789012@gs-project-accounts.iam.gserviceaccount.com" }
  }

  override_data {
    target = module.platform.data.google_bigquery_default_service_account.bq
    values = { email = "bq-123456789012@bigquery-encryption.iam.gserviceaccount.com" }
  }

  override_data {
    target = module.platform.data.google_logging_project_cmek_settings.logging
    values = { service_account_id = "service-123456789012@gcp-sa-logging.iam.gserviceaccount.com" }
  }
}

mock_provider "google-beta" {}

mock_provider "local" {}

run "phase1_dry_run_render_only" {
  command = plan

  assert {
    condition     = length(module.platform.unresolved_placeholders) > 0
    error_message = "dev fixture still carries SLB placeholders"
  }

  assert {
    condition     = module.platform.access_policy_enforcement_mode == "DRY_RUN"
    error_message = "every environment starts in DRY_RUN"
  }

  assert {
    condition     = module.platform.access_policies_applied == false
    error_message = "policies must not be bound before the Agent Runtime engines exist"
  }
}

run "phase3_policies_bound" {
  command = plan

  variables {
    org_id                 = "123456789012"
    agent_runtime_deployed = true
    reasoning_engine_ids = {
      pm_agent_core    = "1111111111111111111"
      signal_ingestion = "2222222222222222222"
      commit_executor  = "3333333333333333333"
    }
  }

  assert {
    condition     = module.platform.access_policies_applied
    error_message = "policies are bound once engines exist and principals are resolved"
  }

  assert {
    condition     = length(module.platform.access_policy_pairs) == 27
    error_message = "authorization matrix (docs/08 §3.2) must yield 27 (identity, destination) pairs"
  }

  assert {
    condition = alltrue([
      for k in keys(module.platform.access_policy_pairs) :
      !(startswith(k, "core__") || startswith(k, "ingest__")) || !(endswith(k, "-write") || endswith(k, "svc-prototype-renderer"))
    ])
    error_message = "AID-CORE / AID-INGEST must never reach a write server"
  }

  assert {
    condition     = join(",", module.platform.access_policy_pairs["core__mcp-ledger"].tools) == "propose,query,get_action"
    error_message = "AID-CORE ledger tools must be exactly propose, query, get_action"
  }

  assert {
    condition     = join(",", module.platform.access_policy_pairs["commit__mcp-ledger"].tools) == "consume_token,record,get_action,release_validation_package,record_trace_links"
    error_message = "AID-COMMIT ledger tools must match the action catalog"
  }
}

run "uap_v2_variant" {
  command = plan

  variables {
    org_id                 = "123456789012"
    agent_runtime_deployed = true
    policy_model           = "UAP_V2"
  }

  assert {
    condition     = module.platform.access_policies_applied
    error_message = "UAP variant must plan"
  }
}

run "strict_env_rejects_placeholders" {
  command = plan

  variables {
    strict_placeholders = true
  }

  expect_failures = [output.unresolved_placeholders]
}
