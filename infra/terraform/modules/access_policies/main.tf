# Tool-level authorization at Agent Gateway egress (docs/08 §3.2-3.3, R2 layer L2) + rollout switch (dry-run).
#
# Provider support CONFIRMED (hashicorp/google):
#   google_iap_agent_registry_{mcp_server,agent,endpoint}_iam_policy  (>= 7.39/7.40) == `gcloud iap web set-iam-policy
#       <file> --resource-type=agent-registry --mcp-server|--agent|--endpoint=<ID> --region=<R>`
#   google_network_services_authz_extension (service iap.googleapis.com, metadata iamEnforcementMode = DRY_RUN)
#   google_iam_project_access_policy + google_iam_projects_policy_binding (UAP, policy_kind ACCESS)   (>= 7.44 / 8.6)
#
# What this module does:
#   1. Renders one IAP egress policy JSON per (identity, destination) pair of the authorization matrix, in the exact
#      docs/08 §3.3 shape (role roles/iap.egressor; CEL on iap.googleapis.com/mcp.toolName / mcp.tool.isReadOnly).
#   2. Renders one MERGED file per destination: set-iam-policy REPLACES the whole policy of a registry resource, so
#      applying per-pair files one after another would clobber earlier bindings. The merged file is what is applied.
#   3. Applies the merged policies natively (default) or with gcloud (apply_mode = "gcloud") once apply_enabled is
#      true and every agent principal is resolved (no PLACEHOLDER tokens). Until then it renders for review only.
#   4. Owns the gateway's IAP REQUEST_AUTHZ extension, i.e. the DRY_RUN <-> enforce switch (var.dry_run).
#   5. Optionally (policy_model = "UAP_V2") applies the same matrix as an IAM Unified Access Policy instead.

locals {
  ident = {
    core   = var.agent_principals.core
    ingest = var.agent_principals.ingest
    commit = var.agent_principals.commit
  }
  aid          = { core = "AID-CORE", ingest = "AID-INGEST", commit = "AID-COMMIT" }
  title_prefix = { core = "core", ingest = "ingest", commit = "executor" }

  principals_resolved = alltrue([for p in values(local.ident) : !strcontains(p, "PLACEHOLDER:")])
  can_apply           = var.apply_enabled && local.principals_resolved

  apply_v1_native = local.can_apply && var.policy_model == "IAP_ALLOW_V1" && var.apply_mode == "native"
  apply_v1_gcloud = local.can_apply && var.policy_model == "IAP_ALLOW_V1" && var.apply_mode == "gcloud"
  apply_v2        = local.can_apply && var.policy_model == "UAP_V2"

  rendered_dir     = coalesce(var.rendered_dir, "${path.root}/rendered/iap-egress-policies")
  registered       = keys(var.mcp_server_ids)
  tool_names       = { for s, tools in var.tools_by_server : s => [for t in tools : t.name] }
  write_servers    = [for s in keys(var.write_tools) : s if contains(local.registered, s)]
  executor_present = contains(keys(var.endpoint_ids), var.a2a_executor_endpoint)
  essential_eps    = [for e in keys(var.endpoint_ids) : e if e != var.a2a_executor_endpoint]

  # ------------------------------------------------------------ authorization matrix -> pairs (docs/08 §3.2)
  # mode: readonly = CEL isReadOnly == true | tools = CEL toolName in [...] | any = identity-only (A2A endpoint)
  pairs_read = merge([
    for id in ["core", "ingest"] : {
      for s in var.read_servers : "${id}__${s}" => { identity = id, dest = s, kind = "mcp", mode = "readonly", tools = [] }
      if contains(local.registered, s)
    }
  ]...)

  pairs_core_internal = {
    for s in var.internal_servers : "core__${s}" => {
      identity = "core", dest = s, kind = "mcp", mode = "tools"
      tools    = [for t in lookup(local.tool_names, s, []) : t if !contains(var.core_internal_excluded_tools, t)]
    } if contains(local.registered, s)
  }

  pairs_core_ledger = contains(local.registered, "mcp-ledger") ? {
    "core__mcp-ledger" = { identity = "core", dest = "mcp-ledger", kind = "mcp", mode = "tools", tools = var.core_ledger_tools }
  } : {}

  # commit-executor is reached over A2A through the gateway; it is registered as an Agent Registry ENDPOINT (pattern
  # documented for Agent Runtime targets). A2A skill-level CEL attributes are UNCONFIRMED -> identity-only binding;
  # the executor exposes a single `execute` skill.
  pairs_core_a2a = local.executor_present ? {
    "core__${var.a2a_executor_endpoint}" = { identity = "core", dest = var.a2a_executor_endpoint, kind = "endpoint", mode = "any", tools = ["execute"] }
  } : {}

  pairs_ingest_internal = {
    for s, tools in var.ingest_internal_tools : "ingest__${s}" => { identity = "ingest", dest = s, kind = "mcp", mode = "tools", tools = tools }
    if contains(local.registered, s)
  }

  pairs_commit_internal = {
    for s, tools in var.commit_internal_tools : "commit__${s}" => { identity = "commit", dest = s, kind = "mcp", mode = "tools", tools = tools }
    if contains(local.registered, s)
  }

  pairs_commit_ledger = contains(local.registered, "mcp-ledger") ? {
    "commit__mcp-ledger" = { identity = "commit", dest = "mcp-ledger", kind = "mcp", mode = "tools", tools = var.commit_ledger_tools }
  } : {}

  pairs_commit_write = {
    for s, tools in var.write_tools : "commit__${s}" => { identity = "commit", dest = s, kind = "mcp", mode = "tools", tools = tools }
    if contains(local.registered, s)
  }

  pairs = merge(
    local.pairs_read, local.pairs_core_internal, local.pairs_core_ledger, local.pairs_core_a2a,
    local.pairs_ingest_internal, local.pairs_commit_internal, local.pairs_commit_ledger, local.pairs_commit_write,
  )

  # ------------------------------------------------------------ V1 bindings (exact docs/08 §3.3 CEL)
  v1_base = "api.getAttribute('iap.googleapis.com/request.auth.type', '') == 'MCP'"

  v1_bindings = {
    for k, p in local.pairs : k => {
      dest    = p.dest
      kind    = p.kind
      members = [local.ident[p.identity]]
      title   = p.mode == "any" ? null : "${local.title_prefix[p.identity]}-${p.mode}-${trimprefix(p.dest, "mcp-")}"
      expression = (
        p.mode == "readonly" ? "${local.v1_base} && api.getAttribute('iap.googleapis.com/mcp.tool.isReadOnly', false) == true" :
        p.mode == "tools" ? "${local.v1_base} && api.getAttribute('iap.googleapis.com/mcp.toolName', '') in [${join(", ", [for t in p.tools : "'${t}'"])}]" :
        null
      )
    }
  }

  # Essential platform endpoints: every agent identity, no condition.
  essential_bindings = {
    for e in local.essential_eps : e => [{
      dest = e, kind = "endpoint", members = values(local.ident), title = null, expression = null
    }]
  }

  dests         = distinct([for b in values(local.v1_bindings) : b.dest])
  dest_bindings = { for d in local.dests : d => [for b in values(local.v1_bindings) : b if b.dest == d] }

  mcp_dest_binds      = { for d, bs in local.dest_bindings : d => bs if bs[0].kind == "mcp" }
  endpoint_dest_binds = merge(local.essential_bindings, { for d, bs in local.dest_bindings : d => bs if bs[0].kind == "endpoint" })
  merged_files        = merge(local.dest_bindings, local.essential_bindings)

  gcloud_targets = merge(
    { for d, _ in local.mcp_dest_binds : d => { flag = "mcp-server", id = var.mcp_server_ids[d] } },
    { for d, _ in local.endpoint_dest_binds : d => { flag = "endpoint", id = var.endpoint_ids[d] } },
  )
}

# ================================================================= rendering (always; review artefacts + gcloud input)
resource "local_file" "pair_policy" {
  for_each        = local.v1_bindings
  filename        = "${local.rendered_dir}/${var.env}/pairs/${each.key}.json"
  content         = templatefile("${path.module}/templates/iap_egress_policy.json.tftpl", { bindings = [each.value] })
  file_permission = "0644"
}

resource "local_file" "merged_policy" {
  for_each        = local.merged_files
  filename        = "${local.rendered_dir}/${var.env}/apply/${each.key}.json"
  content         = templatefile("${path.module}/templates/iap_egress_policy.json.tftpl", { bindings = each.value })
  file_permission = "0644"
}

# ================================================================= rollout switch: IAP REQUEST_AUTHZ on the egress gateway
# Without an attached authorization policy an Agent-to-Anywhere gateway is default-deny for all egress.
resource "google_network_services_authz_extension" "iap" {
  project   = var.project_id
  location  = var.region
  name      = "agw-${var.env}-iap-request-authz"
  service   = "iap.googleapis.com"
  timeout   = var.iap_authz_timeout
  fail_open = false
  metadata = merge(
    { iapPolicyVersion = var.policy_model == "UAP_V2" ? "V2" : "V1" },
    var.dry_run ? { iamEnforcementMode = "DRY_RUN" } : {},
  )
}

resource "google_network_security_authz_policy" "iap" {
  project        = var.project_id
  location       = var.region
  name           = "agw-${var.env}-iap-request-authz"
  description    = "IAP evaluates agent-identity egress policies (${var.dry_run ? "DRY_RUN: audit only" : "ENFORCED"})"
  policy_profile = "REQUEST_AUTHZ"
  action         = "CUSTOM"

  target {
    resources = [var.egress_gateway_id]
  }

  custom_provider {
    authz_extension {
      resources = [google_network_services_authz_extension.iap.id]
    }
  }
}

# ================================================================= V1 apply — native (authoritative per registry resource)
data "google_iam_policy" "mcp" {
  for_each = { for d, bs in local.mcp_dest_binds : d => bs if local.apply_v1_native }

  dynamic "binding" {
    for_each = each.value
    content {
      role    = "roles/iap.egressor"
      members = binding.value.members
      dynamic "condition" {
        for_each = binding.value.expression == null ? [] : [binding.value]
        content {
          title      = condition.value.title
          expression = condition.value.expression
        }
      }
    }
  }
}

resource "google_iap_agent_registry_mcp_server_iam_policy" "mcp" {
  for_each      = { for d, bs in local.mcp_dest_binds : d => bs if local.apply_v1_native }
  project       = var.project_id
  location      = var.region
  mcp_server_id = var.mcp_server_ids[each.key]
  policy_data   = data.google_iam_policy.mcp[each.key].policy_data
}

data "google_iam_policy" "endpoint" {
  for_each = { for e, bs in local.endpoint_dest_binds : e => bs if local.apply_v1_native }

  dynamic "binding" {
    for_each = each.value
    content {
      role    = "roles/iap.egressor"
      members = binding.value.members
    }
  }
}

resource "google_iap_agent_registry_endpoint_iam_policy" "endpoint" {
  for_each    = { for e, bs in local.endpoint_dest_binds : e => bs if local.apply_v1_native }
  project     = var.project_id
  location    = var.region
  endpoint_id = var.endpoint_ids[each.key]
  policy_data = data.google_iam_policy.endpoint[each.key].policy_data
}

# ================================================================= V1 apply — gcloud fallback (documented command)
# Same effect as the native resources above, using the rendered merged files (fallback while the Preview churns).
resource "terraform_data" "gcloud_set_iam_policy" {
  for_each = { for d, t in local.gcloud_targets : d => t if local.apply_v1_gcloud }

  triggers_replace = [
    local_file.merged_policy[each.key].content_sha256,
    each.value.id,
  ]

  provisioner "local-exec" {
    command = join(" ", [
      "gcloud iap web set-iam-policy", local_file.merged_policy[each.key].filename,
      "--resource-type=agent-registry", "--${each.value.flag}=${each.value.id}",
      "--region=${var.region}", "--project=${var.project_id}", "--quiet",
    ])
  }
}

# ================================================================= V2 (UAP) — same matrix, plus an explicit DENY rule
locals {
  v2_dest_name = merge(var.mcp_server_resources, var.endpoint_resources)

  v2_rules = concat(
    [
      for k, p in local.pairs : {
        description = "${local.aid[p.identity]} -> ${p.dest} (${p.mode})"
        effect      = "ALLOW"
        principals  = [local.ident[p.identity]]
        expression = (
          p.kind == "endpoint" ? "destination.agent_registry.endpoint.name == '${lookup(local.v2_dest_name, p.dest, "")}'" :
          p.mode == "readonly" ? "destination.agent_registry.mcp_server.name == '${lookup(local.v2_dest_name, p.dest, "")}' && destination.agent_registry.mcp_server.tool.annotations.read_only_hint == true" :
          "destination.agent_registry.mcp_server.name == '${lookup(local.v2_dest_name, p.dest, "")}' && destination.agent_registry.mcp_server.tool.name in [${join(", ", [for t in p.tools : "'${t}'"])}]"
        )
      }
    ],
    length(local.essential_eps) == 0 ? [] : [{
      description = "all agent identities -> essential platform endpoints"
      effect      = "ALLOW"
      principals  = values(local.ident)
      expression  = "destination.agent_registry.endpoint.name in [${join(", ", [for e in local.essential_eps : "'${lookup(var.endpoint_resources, e, "")}'"])}]"
    }],
    length(local.write_servers) == 0 ? [] : [{
      description = "AID-CORE and AID-INGEST may never reach write servers (L2, docs/03 §4)"
      effect      = "DENY"
      principals  = [local.ident.core, local.ident.ingest]
      expression  = "destination.agent_registry.mcp_server.name in [${join(", ", [for s in local.write_servers : "'${lookup(local.v2_dest_name, s, "")}'"])}]"
    }],
  )
}

resource "local_file" "uap_rules" {
  count           = var.policy_model == "UAP_V2" ? 1 : 0
  filename        = "${local.rendered_dir}/${var.env}/uap/agent-egress-rules.json"
  content         = jsonencode(local.v2_rules)
  file_permission = "0644"
}

# The managed constraint name differs between docs (…disableAccessPolicyBindings) and the provider example
# (…disableAccessPolicyBinding); the provider spelling is used here — verify before enabling.
resource "google_org_policy_policy" "allow_access_policy_binding" {
  count  = local.apply_v2 && var.manage_access_policy_binding_org_policy ? 1 : 0
  name   = "projects/${var.project_id}/policies/iam.managed.disableAccessPolicyBinding"
  parent = "projects/${var.project_id}"

  spec {
    rules {
      enforce = "FALSE"
    }
  }
}

resource "google_iam_project_access_policy" "agent_egress" {
  count            = local.apply_v2 ? 1 : 0
  project          = var.project_id
  location         = "global"
  access_policy_id = "pm-agent-${var.env}-agent-egress"
  display_name     = "PM Agent ${var.env} agent egress matrix"

  details {
    dynamic "rules" {
      for_each = local.v2_rules
      content {
        description = rules.value.description
        effect      = rules.value.effect
        principals  = rules.value.principals
        operation {
          permissions = ["iap.googleapis.com/resources.egressViaIAP"]
        }
        conditions {
          service    = "iap.googleapis.com"
          expression = rules.value.expression
        }
      }
    }
  }

  depends_on = [google_org_policy_policy.allow_access_policy_binding]
}

resource "google_iam_projects_policy_binding" "agent_egress" {
  count             = local.apply_v2 ? 1 : 0
  project           = var.project_id
  location          = "global"
  policy_binding_id = "pm-agent-${var.env}-agent-egress"
  display_name      = "PM Agent ${var.env} egress gateway"
  policy_kind       = "ACCESS"
  policy            = "projects/${var.project_id}/locations/global/accessPolicies/${google_iam_project_access_policy.agent_egress[0].access_policy_id}"

  target {
    resource = "//networkservices.googleapis.com/${var.egress_gateway_id}"
  }
}

# ================================================================= Principal Access Boundary (optional, org-scoped)
locals {
  create_pab = var.enable_pab && !startswith(var.org_id, "PLACEHOLDER:")
}

resource "google_iam_principal_access_boundary_policy" "pab" {
  count                               = local.create_pab ? 1 : 0
  organization                        = var.org_id
  location                            = "global"
  principal_access_boundary_policy_id = "pm-agent-${var.env}-pab"
  display_name                        = "PM Agent ${var.env} principal access boundary"

  details {
    enforcement_version = "latest"
    rules {
      description = "Principals of the PM Agent project may only access resources in that project"
      effect      = "ALLOW"
      resources   = ["//cloudresourcemanager.googleapis.com/projects/${var.project_id}"]
    }
  }
}

resource "google_iam_projects_policy_binding" "pab" {
  count             = local.create_pab ? 1 : 0
  project           = var.project_id
  location          = "global"
  policy_binding_id = "pm-agent-${var.env}-pab"
  display_name      = "PM Agent ${var.env} PAB"
  policy_kind       = "PRINCIPAL_ACCESS_BOUNDARY"
  policy            = "organizations/${var.org_id}/locations/global/principalAccessBoundaryPolicies/${google_iam_principal_access_boundary_policy.pab[0].principal_access_boundary_policy_id}"

  target {
    principal_set = "//cloudresourcemanager.googleapis.com/projects/${var.project_id}"
  }
}
