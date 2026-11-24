# infra/ — Terraform for the PM Agent reference architecture

Implements docs/12 §3 (Terraform layout) for the planes in docs/03, the identities/policies in docs/08 and the stores
in docs/10 §7. Region `us-central1`; dev project `chayan-genai-bb`. Kubernetes and container assets are in
[`deploy/`](../deploy); Agent Runtime deployment is in [`deploy/agent_runtime/README.md`](../deploy/agent_runtime/README.md).

```text
infra/terraform/
  modules/
    network/  perimeter/  kms/  data/  logging/  identity/  agent_registry/  agent_gateway/
    access_policies/  model_armor/  cloud_run_service/  gke/  observability/  ci/  platform/
  envs/{dev,stage,prod}/   # thin roots: identical main/variables/outputs/versions; only terraform.tfvars + backend differ
```

Providers: `hashicorp/google` + `google-beta` **>= 8.6.0, < 9** (agent connectivity template needs 8.4, UAP policy
binding `target.resource` needs 8.6), `hashicorp/local` >= 2.5, Terraform >= 1.6. Validated with Terraform 1.17.2 +
google 8.12.0: `terraform validate` passes for all three envs, and `envs/dev/tests/plan_offline.tftest.hcl`
(`terraform test`, mocked providers, no credentials) plans the whole composition — 476 resources in phase 1, 534 in
phase 3 — and checks the authorization matrix, the secret/SDP wiring and the strict-placeholder guard.

## 1. Module map

| Module | What it creates | Key resources |
|---|---|---|
| `network` | VPC, workloads subnet (+GKE ranges), PSC subnets + network attachments for Agent Runtime (PSC-I) and Agent Gateway egress, proxy-only subnet, Cloud NAT with **static egress IPs**, firewall, private DNS `pm-agent.internal.` | `google_compute_*`, `google_dns_*` |
| `perimeter` | VPC-SC perimeter, **explicit dry-run spec**; enforced `status` only when `enforce = true` | `google_access_context_manager_service_perimeter` |
| `kms` | Key ring `pm-agent-<env>`: `approval-token-signer` + `verifier-report-signer` (ASYMMETRIC_SIGN, EC_SIGN_P256_SHA256, HSM), `sdp-wrap`, `cmek-<service>` ×9. `roles/cloudkms.signer` is authoritative: **only** SA-LEDGER / SA-VERIFIER; every component gets viewer + publicKeyViewer on both; `cryptoKeyDecrypter` on `sdp-wrap` only for the 7 redacting servers | `google_kms_*` |
| `data` | AlloyDB (PSC-only, CMEK, IAM DB auth, PITR, read pool, optional DR secondary), GCS artifacts + **ledger exports with Bucket Lock**, BigQuery `pm_agent_analytics`, Pub/Sub `signal-change-hints`, `signal-ingested`, `problem-updated`, `ledger-events` + `-dlq` topics, per-workload Secret Manager secrets (owner-only accessor; PLACEHOLDER bootstrap versions for webhook secrets and `sdp-wrapped-key`) | `google_alloydb_*`, `google_storage_bucket`, `google_bigquery_dataset`, `google_pubsub_*`, `google_secret_manager_*` |
| `logging` | Log bucket `pm-agent-ledger` (locked, Log Analytics, CMEK) + linked BigQuery dataset + mirror sink | `google_logging_project_bucket_config`, `google_logging_linked_dataset` |
| `identity` | One SA per Cloud Run service (15), one GSA per GKE workload, baseline telemetry roles, Workforce Identity Federation pool `slb` + OIDC provider (org-scoped, skipped while Q-10/Q-08 are placeholders) | `google_service_account`, `google_iam_workforce_pool*` |
| `agent_registry` | MCP servers registered **manually with TOOL_SPEC** (`toolspecs/*.json`, annotations = security contract), essential Google API endpoints, commit-executor endpoint | `google_agent_registry_service` |
| `agent_gateway` | Egress (AGENT_TO_ANYWHERE, connectivity template **vpc_egress = ALL_TRAFFIC**) and ingress (CLIENT_TO_AGENT) gateways; Model Armor CONTENT_AUTHZ bindings; service-agent roles | `google_network_services_agent_gateway`, `..._agent_connectivity_template`, `..._authz_extension`, `google_network_security_authz_policy` |
| `access_policies` | IAP egress policies from the docs/08 §3.2 matrix (rendered per pair + merged per destination, applied natively), the **DRY_RUN switch** (IAP REQUEST_AUTHZ extension), optional UAP (V2) and PAB | `google_iap_agent_registry_{mcp_server,endpoint}_iam_policy`, `google_iam_project_access_policy`, `google_iam_projects_policy_binding` |
| `model_armor` | `ma-pm-prompt`, `ma-tool-response` (SDP advanced mode with our inspect/de-identify templates; custom infoTypes read from `config/pm_agent.yaml`) | `google_model_armor_template`, `google_data_loss_prevention_*_template` |
| `cloud_run_service` | One Cloud Run v2 service: per-service SA, `ingress` internal by default, built-in IAP, authoritative `run.invoker` = IAP service agent only (allUsers rejected by validation), optional `functional_type` | `google_cloud_run_v2_service` (google-beta) |
| `gke` | Autopilot private cluster (private endpoint, Binary Authorization, CMEK secrets, Gateway API), KSA→GSA bindings, Cloud Armor policy + static IP for the webhook LB | `google_container_cluster`, `google_compute_security_policy` |
| `observability` | SEV-1 log-based metrics + alerts (TOOL_CALL_DENIED, uncited claims, chain mismatch, commit-without-token), Workbench/Ledger SLOs, dashboard | `google_logging_metric`, `google_monitoring_*` |
| `ci` | Artifact Registry `pm-agent` (immutable tags, CMEK), CI SA (`_AGENT_RUNTIME_SA` in cloudbuild.yaml), optional trigger | `google_artifact_registry_repository`, `google_cloudbuild_trigger` |
| `platform` | Composition used by every env (APIs, service identities, placeholder guard, wiring) | — |

docs/12 lists `cloud_run/` (here `cloud_run_service/`) and `agent_runtime/`: Agent Runtime stays a CI SDK step (the
`google_vertex_ai_reasoning_engine` resource is confirmed — see deploy/agent_runtime/README.md).

## 2. Placeholders

SLB unknowns are `PLACEHOLDER:<Q-id>:<name>` (docs/15). **dev** tolerates them: org-scoped resources (workforce pool,
VPC-SC perimeter, PAB) are skipped, IAP egress policies are rendered but not bound. **stage/prod** set
`strict_placeholders = true`: a precondition on the root output `unresolved_placeholders` fails plan/apply while any
token remains (IdP group ids are read from `config/pm_agent.yaml`), and a `project_id` placeholder fails variable
validation first.

Secret **values** are never Terraform inputs. The webhook secrets (Q-04/Q-05/Q-07/Q-13/Q-02) and `sdp-wrapped-key`
(Q-10) get a bootstrap version containing their PLACEHOLDER token so Cloud Run secret env / External Secrets resolve;
the application treats a token as "not configured" and fails closed (strict in stage/prod). Real values are added as
newer versions out-of-band; then disable (don't destroy) the bootstrap version. These tokens are enforced at runtime,
so they are not part of `unresolved_placeholders`.

| Redacting servers (`mcp-ado`, `mcp-servicenow`, `mcp-pagerduty`, `mcp-msgraph`, `mcp-figma`, `mcp-kb`, `mcp-verifier`) | |
|---|---|
| `PM_AGENT_SDP_WRAPPED_KEY` | Cloud Run secret env from `sdp-wrapped-key` (KMS-wrapped DLP data key) |
| `PM_AGENT_SDP_KMS_KEY` | `sdp-wrap` crypto key name |
| IAM | `roles/dlp.user` (project) + `roles/cloudkms.cryptoKeyDecrypter` on `sdp-wrap` — these seven SAs only |

## 3. Apply order (registry entries + policies in the same apply; dry-run, then enforce)

| Phase | Action | Result |
|---|---|---|
| 0 | `gcloud auth application-default login`; create the state bucket; `terraform init -backend-config=backend.hcl` | — |
| 1 | `terraform apply` (`agent_runtime_deployed = false`) | APIs, network, identities, KMS, data, ledger bucket, Model Armor, Cloud Run (placeholder image), **registry entries**, gateways, IAP authz extension in **DRY_RUN**, GKE, VPC-SC dry-run, observability, CI |
| 2 | CI: build images (`deploy/Dockerfile`), deploy Cloud Run revisions + `deploy/k8s`; deploy the 3 Agent Runtime engines with `identity_type=AGENT_IDENTITY` + `agent_gateway_config` (immutable) | engines bound to gateways (dry-run => nothing blocked) |
| 3 | Set `agent_runtime_deployed = true` + `reasoning_engine_ids`; `terraform apply` | commit-executor endpoint registered **and** every egress policy bound in the same apply; executor invoke IAM; reasoning-engine service-agent grants |
| 4 | Soak: review would-be denials (Agent Gateway dry-run logs / dry-run report). Exit criterion: zero unexpected denials, incl. MCP `initialize` / `tools/list` | — |
| 5 | `access_policy_dry_run = false` → apply (removes `iamEnforcementMode`); later `vpcsc_enforce = true`. Rollback = flip back | enforced (docs/12 §1 target for stage/prod) |

> [!CAUTION]
> `lock_ledger_retention = true` (stage/prod default) locks the log bucket retention and the Bucket Lock on ledger
> exports for 7 years — **irreversible**. dev sets it to `false`.

## 4. Provider support: native vs UNCONFIRMED

Every capability the brief flagged has a **native** resource (evidence: provider docs + CHANGELOG at
github.com/hashicorp/terraform-provider-google, and the Terraform tabs of docs.cloud.google.com/gemini-enterprise-agent-platform
`govern/gateways/set-up-agent-gateway`, `.../delegate-authorization`, `govern/policies/configure-iam-policies-uap`,
`scale/runtime/agent-gateway-runtime-deploy`): Agent Gateway (7.37/7.38 GA), connectivity template (8.4.0), Agent
Registry service (7.39), IAP agent-registry IAM (7.39/7.40), IAM access policy (7.44), Model Armor template (6.43),
Cloud Run `iap_enabled` (7.21), reasoning engine `agent_gateway_config` (8.1.0). The only `terraform_data` + gcloud
`local-exec` is the optional `apply_mode = "gcloud"` fallback (`gcloud iap web set-iam-policy <file>
--resource-type=agent-registry --mcp-server=<ID> --region=<R>`).

| UNCONFIRMED item | Handling |
|---|---|
| Cloud Run `functional_type = FUNCTIONAL_TYPE_MCP_SERVER` (google-beta only, Preview); auto-registration records name + type only and would duplicate the manual TOOL_SPEC entry | off by default (`mcp_functional_type_enabled`); manual registration |
| A2A skill-level CEL attributes (`execute` on commit-executor) | identity-only binding on the endpoint + engine-level `roles/aiplatform.user` for AID-CORE only |
| Principal Cloud Run IAP sees for gateway-forwarded MCP calls | agent principals as IAP accessors; verify in soak |
| `initialize` / `tools/list` under tool-scoped CEL (`mcp.tool.isReadOnly` defaults to false) | verify in dry-run; UAP offers `destination.agent_registry.mcp_server.method` |
| Agent Runtime engine ids user-chosen vs server-assigned | `reasoning_engine_ids` variable fed back in phase 3 |
| Agent Identity trust domain org vs project (`agents.global.proj-<PNUM>…` appears in current docs) | `agent_identity_trust_domain` override |
| `iam.managed.disableAccessPolicyBinding(s)` constraint name (docs vs provider example differ) | provider spelling, opt-in only |
| PAB principal set covering agent identities | `enable_pab = false` |
| `google_project_service_identity` for modelarmor / dlp (beta) | used; fails loudly if unsupported |
| CMEK on a Log Analytics–enabled log bucket | enabled; set `kms_key_name = null` if rejected |
| `adk deploy agent_engine` flags for identity/gateway | SDK path documented as primary |
| `registry_resource` format `…/mcpServers/<id>` | id extracted by regex; verify on first apply |
| Cloud Run internal ingress + built-in IAP for traffic arriving from the gateway's PSC interface | design per docs/12; verify in dev soak |
| Workforce pool id: docs/07 shows `workforcePools/slb`, but the API requires 6–63 chars | default `slb-pm-agent` (reuse SLB's pool if Q-08 says so) |

### Caller identity for Cloud Run MCP servers (mcp-ledger `proposed_by` / `consumer` / `actor_id`)

These must come from the authenticated caller, never from tool arguments.

* **Confirmed (IAP signed-headers docs, docs.cloud.google.com/iap/docs/signed-headers-howto):** IAP adds
  `x-goog-iap-jwt-assertion`, an ES256 JWT. Verify it against https://www.gstatic.com/iap/verify/public_key-jwk with
  `iss = https://cloud.google.com/iap` and, for Cloud Run, `aud = /projects/<PROJECT_NUMBER>/locations/<region>/services/<service>`.
  `x-goog-authenticated-user-{email,id}` are unsigned — never trust them alone. IAP strips client-supplied `x-goog-*`
  headers.
* **Workforce users (Workbench → `mcp-ledger.decide`):** the JWT carries `sub` (`sts.google.com:…`),
  `workforce_identity.iam_principal` (`principal://iam.googleapis.com/locations/global/workforcePools/<pool>/subject/<id>`)
  and `email`, the latter only with the `google.email` attribute mapping (now set in `modules/identity`). IAP does
  **not** pass IdP groups, so `config.approver_rules` group checks need another source (e.g. a Graph group lookup — Q-08).
* **UNCONFIRMED — agent calls forwarded by Agent Gateway egress:** which principal that JWT carries (expected: the
  Agent Identity `principal://agents.global…/reasoningEngines/<id>`), and whether the gateway adds its own identity
  header. Verify in the dev soak. Until then mcp-ledger should derive actors only from a verified
  `x-goog-iap-jwt-assertion` and reject calls without one.

## 5. Decisions to review (spec vs current docs)

* **docs/08 §3.3 uses legacy IAP allow policies (iapPolicyVersion V1).** Current Agent Gateway docs recommend
  Unified Access Policies (V2); under V2, `roles/iap.egressor` bindings no longer open traffic. Both are implemented;
  default `policy_model = "IAP_ALLOW_V1"` matches the approved spec — switching is one variable.
* Per-pair JSON files (docs/08 shape) are review artefacts; **merged per-destination** policies are applied because
  set-iam-policy replaces a resource's whole policy.
* commit-executor is registered as an Agent Registry **endpoint** (the documented pattern for Agent Runtime targets).
* pm-workbench / svc-prototype-renderer use Cloud Run built-in IAP with `ingress = ALL`; the docs/12 External HTTPS LB
  front door for the Workbench is a follow-up once the hostname (Q-10) exists.
* Secret Manager secrets live in `data` (a store per docs/10 §7) to keep identity → kms → data acyclic.
* Follow-ups not provisioned: DR-region key ring for the AlloyDB secondary, private-CA certificate for
  `mcp-correlation`, Cloud Scheduler → signal-ingestion via the ingress gateway, Binary Authorization attestor
  policy, BigQuery policy tags, Semantic Governance policies (Preview), Auth Manager bindings (SP-9), and a GKE
  control-plane access path for CI (`gke_master_authorized_cidrs` / private pool, or Config Sync).

## 6. Commands

```bash
cd infra/terraform/envs/dev
terraform init -backend=false && terraform validate && terraform test   # offline checks (no credentials)
cp backend.hcl.example backend.hcl && terraform init -reconfigure -backend-config=backend.hcl
terraform plan -out=tf.plan                                 # rendered policies: ./rendered/iap-egress-policies/dev/
```
