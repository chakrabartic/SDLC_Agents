# 12 — Deployment & Operations

> Concept-to-production delivery on standard GCP serverless (**Cloud Run**, **Agent Runtime**) and **GKE Autopilot**, provisioned by Terraform, promoted through gated CI/CD, observed end-to-end with OpenTelemetry. Region **us-central1** (all components verified available there, incl. Agent Gateway, Sessions, Memory Bank).

---

## 1. Environments

| Env | GCP project | Purpose | Data | Gateway mode |
|---|---|---|---|---|
| `dev` | `chayan-genai-bb` (workspace default) | Engineering, unit/integration, replayed evals | Synthetic + sanitized corpus fixtures only | Dry-run |
| `stage` | SLB-provided (Q-10) | SLB sandbox connectors (ADO/SNOW/PD/Graph/Figma/KB non-prod), UAT, online evals | SLB non-prod | Enforce |
| `prod` | SLB-provided (Q-10) | Pilot PMs → GA | SLB prod (read) + approved writes | Enforce |

Each env is its own VPC-SC perimeter, KMS key ring, Workforce/Workload Identity pools, and Agent Registry.

---

## 2. Deployment units

| Unit | Runtime | Build | Scaling / resources | Identity | Ingress |
|---|---|---|---|---|---|
| `pm-agent-core` | Agent Runtime | Container image (Artifact Registry) | min 1 · max 20 instances; `container_concurrency` tuned by load test | `AID-CORE` | Agent Gateway ingress only |
| `signal-ingestion` | Agent Runtime | Container image | min 0–1 · max 5 | `AID-INGEST` | Cloud Scheduler via Agent Gateway ingress |
| `commit-executor` | Agent Runtime | Container image | min 1 · max 3 (low volume, latency-sensitive) | `AID-COMMIT` | A2A from `AID-CORE` only |
| `mcp-*` connectors (×10: 6 read + 4 write), `mcp-store`, `mcp-verifier`, `mcp-ledger` | Cloud Run | Container (distroless Python 3.13) | min 0–1 (1 for verifier/ledger/store), max 20–50, CPU always-allocated for verifier | `SA-<svc>` | `--ingress internal`, IAP-protected MCP (`--iap --functional-type=mcp-server`, Preview) |
| `svc-prototype-renderer` | Cloud Run | Container | min 0 · max 5 | `SA-RENDER` | IAP (plugin users) |
| `pm-workbench` | Cloud Run | Container (Angular SPA + Python BFF) | min 1 · max 10, session affinity for SSE | `SA-WORKBENCH` | External HTTPS LB + IAP |
| `signal-event-gateway` | GKE Autopilot | Container | HPA 2–10 on RPS/CPU; PDB | KSA→WIF | External HTTPS LB + Cloud Armor |
| `correlation-engine` | GKE Autopilot | Container | Deployment 2 replicas (memory-optimized) + CronJob (re-cluster nightly) | KSA→WIF | Internal LB (PSC to Agent Gateway egress) |
| `dls-catalog-sync` | GKE Autopilot CronJob | Container | hourly + on `LIBRARY_PUBLISH` hint | KSA→WIF | none |

Agent Runtime creation (verified config keys):

```python
# design sketch — CI deploy step (google-cloud-aiplatform[agent_engines,adk])
remote = client.runtimes.create(
    agent=app,                                   # ADK App (pm_agent_core)
    config={
        "display_name": "pm-agent-core",
        "agent_framework": "google-adk",
        "identity_type": "AGENT_IDENTITY",       # immutable — set on first deploy
        "agent_gateway_config": {...},           # immutable — required for Gateway + Semantic Governance
        "psc_interface_config": {...},           # private VPC egress (max_instances capped at 100 with PSC-I/VPC-SC)
        "encryption_spec": {"kms_key_name": "..."},
        "min_instances": 1, "max_instances": 20,
        "resource_limits": {"cpu": "4", "memory": "8Gi"},
        "env_vars": {...},                       # never GOOGLE_CLOUD_PROJECT / LOCATION (platform-provided)
    },
)
```

CLI alternative: `adk deploy agent_engine --project <p> --region us-central1 --display_name pm-agent-core --adk_app agent.app --otel_to_cloud <dir>` (command keeps the legacy name). **Flag:** how `client` is constructed in the new `google-cloud-agentplatform` 2.x SDK (breaking split, 2026-09-18) is UNCONFIRMED — resolved in M0.

---

## 3. Infrastructure as code (Terraform)

```text
infra/terraform/
  modules/
    network/            # VPC, subnets, PSC, Cloud NAT (static egress IPs), firewall
    perimeter/          # VPC-SC service perimeter + access levels
    kms/                # key rings: cmek, approval-token-signer (EC P-256), report-signer, sdp-wrap
    data/               # AlloyDB (+ read pool, cross-region replica), GCS (+ Bucket Lock), BigQuery, Pub/Sub
    logging/            # locked log bucket pm-agent-ledger + linked BigQuery dataset
    identity/           # Workforce pool (SLB IdP), IAP brand/clients, workload identity pools, service accounts
    agent_registry/     # MCP server / agent / endpoint registrations
    agent_gateway/      # ingress + egress gateways, connectivity template (ALL_TRAFFIC), Model Armor bindings
    access_policies/    # IAM UAP / IAP egress policies (CEL) + PAB — dry-run flag per env
    model_armor/        # templates ma-pm-prompt, ma-tool-response
    cloud_run/          # one module instance per service
    gke/                # Autopilot private cluster, Binary Authorization, workloads (Helm/Kustomize via Config Sync)
    agent_runtime/      # reasoning engines (if provider support is confirmed; else CI SDK step)
    observability/      # dashboards, alert policies, SLOs, log-based metrics
  envs/{dev,stage,prod}/
```

Gateway, registry and IAM-policy resources are created **in the same apply** (policy bindings to unregistered resources fail with `NOT_FOUND`). Console drift triggers an alert.

---

## 4. CI/CD pipeline (Cloud Build; GitHub Actions + WIF equivalent)

| # | Stage | Gate (must pass) |
|---|---|---|
| 1 | Lint & types | `pylint` (score ≥ 9.5), `mypy --strict` clean |
| 2 | Policy lint | No `GOOGLE_API_KEY` / `google.generativeai`; no `except BaseException`; no `session.events.append`; MCP annotations vs. write capability contract |
| 3 | Unit tests | `pytest` (+ `pytest-asyncio`, `pytest-mock`); coverage ≥ 90 % for `grounding/`, `hitl/`, `connectors/normalize`, ≥ 80 % overall |
| 4 | Contract tests | Connector fixtures; A2UI payload validation against v1.0 + SLB catalog schemas; ledger DB grants (no UPDATE/DELETE); token verification vectors |
| 5 | **Eval gate** (replay mode) | **E3 uncited claims in approved outputs = 0** (hard); E1/E2/E4/E5/E7/E8 thresholds ([13](13-testing-and-evaluation.md)) |
| 6 | **Safety suite** (AC9) | 0 unauthorized writes; 0 accepted forged/replayed tokens; all positive controls succeed |
| 7 | Build & supply chain | SBOM, Artifact Analysis (no critical CVEs), image signing → Binary Authorization attestation |
| 8 | IaC | `terraform plan` reviewed; access policies applied in dry-run first in each env |
| 9 | Deploy `stage` | Smoke tests against SLB sandboxes; online evals (simulated PM sessions) |
| 10 | Release gate (human) | Phase-gate checklist signed ([00](00-methodology-and-phase-gates.md)) |
| 11 | Deploy `prod` | Progressive: Cloud Run traffic 10 % → 50 % → 100 %; Agent Runtime blue/green (new engine version behind the gateway; revisions/traffic-split are Preview) |

Model, ADK, A2UI-SDK or prompt changes always re-run stages 5–6 on the full golden set.

---

## 5. Observability

| Signal | Source | Destination |
|---|---|---|
| Traces | ADK OpenTelemetry (on by default on Agent Runtime; `--otel_to_cloud`), MCP servers (OTel), Agent Gateway telemetry | Cloud Trace + Agent Observability (topology, traces) |
| Logs | Structured JSON (ids, hashes, `trace_id`; no content) | Cloud Logging; ledger mirror in locked bucket |
| Agent analytics | `BigQueryAgentAnalyticsPlugin` | BigQuery `pm_agent_analytics` |
| Metrics | Custom (verifier, ledger, connectors), gen_ai metrics (`GOOGLE_CLOUD_AGENT_ENGINE_ENABLE_TELEMETRY`, ADK ≥ 2.6) | Cloud Monitoring dashboards |

### 5.1 SLOs & alerts

| SLO | Target |
|---|---|
| Workbench availability | 99.5 % monthly |
| Draft-ready latency (single problem) | p95 ≤ 45 s |
| Ingestion freshness (webhook-hinted sources) | p95 ≤ 15 min |
| Ledger write availability | 99.9 % |
| **Uncited claims in approved outputs** | **0** (any occurrence = SEV-1) |
| Unauthorized write attempts reaching a write server | 0 (alert on any `TOOL_CALL_DENIED` from `AID-CORE`/`AID-INGEST` to write servers) |

### 5.2 KPI dashboard (requirements §3.3)

Signal-to-problem linkage rate · requirement first-pass acceptance · context-gathering time (PM-reported + session analytics) · knowledge-article reuse · roadmap evidence completeness · grounding integrity (100 %).

---

## 6. Runbooks (to be written in M7)

Connector outage & credential rotation · Gateway policy rollout/rollback · model upgrade (eval-gated) · ADK / A2UI SDK upgrade · DLS library major change · ledger integrity alert (chain mismatch) · DR failover · PII/secret exposure incident · Figma MCP admission (switch renderer path A → B) · Graph subscription renewal failures.

## 7. Resilience, backup, DR

AlloyDB automated backups + PITR + cross-region replica; GCS exports Bucket-Locked (dual-region where residency allows); Terraform re-provision of stateless tiers; Agent Runtime redeploy from images. Targets (to confirm with SLB): **RPO ≤ 15 min, RTO ≤ 4 h**.

## 8. Cost levers

Flash-Lite for routing/classification; context caching for stable instruction+template prefixes; batch embeddings and Batch API for backfills; scale-to-zero for low-traffic MCP servers; bounded evidence packs; per-workflow token budgets with alerts.
