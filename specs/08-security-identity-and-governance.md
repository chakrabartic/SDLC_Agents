# 08 — Security, Identity & Governance

> **Goal.** Least privilege at every hop, cryptographic identity for every agent, deny-by-default for every tool, and no path from untrusted content to a system-of-record write. Implements R2, R6 (redaction), GOV4–GOV7, NFR-1/2/3/11, IR1, DONE-8.

---

## 1. Trust boundaries

```mermaid
flowchart LR
  subgraph Internet
    U["PM browser"]
    V["Vendor webhooks"]
    SaaS["ADO · ServiceNow · PagerDuty ·<br/>Graph · Figma · KB"]
  end
  subgraph Perimeter["VPC-SC perimeter (Google Cloud)"]
    IAP["IAP + Workforce IdF"] --> WB["pm-workbench"]
    WB --> GWI["Agent Gateway ingress<br/>Model Armor"]
    GWI --> CORE["pm-agent-core<br/>AID-CORE"]
    CORE --> GWE["Agent Gateway egress<br/>Registry · IAM UAP · PAB · Model Armor"]
    ING["signal-ingestion<br/>AID-INGEST"] --> GWE
    EXE["commit-executor<br/>AID-COMMIT"] --> GWE
    GWE --> RMCP["read MCP servers"]
    GWE --> WMCP["write MCP servers<br/>(token required)"]
    GWE --> INT["mcp-store · mcp-verifier ·<br/>mcp-ledger · mcp-correlation"]
    WB -- "decide() + IAP JWT" --> INT
    CA["Cloud Armor + LB"] --> EVG["signal-event-gateway"]
  end
  U --> IAP
  V --> CA
  RMCP -- "read-scoped creds" --> SaaS
  WMCP -- "write-scoped creds" --> SaaS
```

Boundaries that matter most: **(B1)** untrusted SaaS content → agents (content is data); **(B2)** agents → tools (Gateway + identity); **(B3)** humans → approvals (IAP, never via LLM); **(B4)** executor → systems of record (token + write-scoped credentials).

---

## 2. Verified platform capabilities used

| Capability | Status (verified 2026-10-12) | Use |
|---|---|---|
| Agent Gateway — ingress (Client-to-Agent) | GA (2026-06-18); **Agent Runtime agents only** | Workbench → `pm-agent-core` |
| Agent Gateway — egress (Agent-to-Anywhere) | GA; Agent Runtime + Gemini Enterprise as sources | Every agent → MCP / agent / endpoint call |
| Agent Identity (SPIFFE, mTLS + DPoP via Context-Aware Access) | GA (2026-04-22); `agentidentity.googleapis.com` API Preview | One identity per Agent Runtime deployment |
| Agent Registry | GA (VPC-SC supported) | Registers MCP servers, agents, endpoints (≤ 5,000 per gateway) |
| IAM Unified Access Policies (allow + deny, CEL on MCP tool attributes, dry-run) | GA (2026-08-31) | Tool-level authorization |
| Principal Access Boundary on agent identities | Documented for egress | Hard ceiling per identity |
| Model Armor on gateways | GA (2026-06-24) | Prompt-injection / jailbreak / sensitive-data / malicious-URL screening |
| Semantic Governance policies (natural-language constraints) | **Preview** — requires agent deployed with `identity_type=AGENT_IDENTITY` **and** `agent_gateway_config` (immutable) | Secondary business-rule guard (not relied on for R2) |
| Custom authorization engines (Service Extensions) | Documented | Optional: approval-token check at the gateway |
| Cloud Run as IAP-protected MCP server (`--functional-type=mcp-server`) | **Preview** | Hosting our MCP servers as gateway destinations |
| "Envoy-based" gateway | **UNCONFIRMED** — docs state enforcement via Service Extensions and IAP; Envoy not mentioned | No design dependency |

---

## 3. Identity model

### 3.1 Workload identities

| Identity | Principal (format verified) | Holds |
|---|---|---|
| `AID-CORE` | `principal://agents.global.org-<ORG_ID>.system.id.goog/resources/aiplatform/projects/<PNUM>/locations/us-central1/reasoningEngines/pm-agent-core` | Read-only connector tools; internal store/verifier/ledger(propose,query); A2A to `commit-executor` |
| `AID-INGEST` | `…/reasoningEngines/signal-ingestion` | Read-only connector tools (broad windows); `store.put_signals`; `correlation.assign_candidates`; `verifier.redact` |
| `AID-COMMIT` | `…/reasoningEngines/commit-executor` | **Only** the write tools by exact name; `ledger.consume_token`/`record`; `verifier.verify_subject` |
| `SA-<service>` | Cloud Run service accounts (one per service) | Third-party credentials for *that* server only; DB role for *that* schema only |
| GKE workloads | `principal://iam.googleapis.com/projects/<PNUM>/locations/global/workloadIdentityPools/<PROJECT>.svc.id.goog/subject/ns/<ns>/sa/<ksa>` (WIF); SPIFFE `spiffe://<PROJECT>.svc.id.goog/ns/<ns>/sa/<ksa>` with managed workload identity (**Preview**) | Pub/Sub publish (event gateway); store read + index write (correlation); Figma REST read (catalog sync) |
| Humans | Workforce Identity Federation pool `slb` (IdP = SLB Entra ID, **Q-08**), through IAP | Workbench access; approvals per `config.approver_rules` |

Agent Runtime deployments are created with `identity_type=AGENT_IDENTITY` and `agent_gateway_config` at **first deploy** (both immutable). Secrets passed as env vars require `secretmanager.versions.access` for the Agent Platform service agent (`service-<PNUM>@gcp-sa-aiplatform.iam.gserviceaccount.com`).

### 3.2 Tool-level authorization matrix (enforced at Agent Gateway egress)

| Source identity → | Read connectors (`mcp-ado`, `mcp-servicenow`, `mcp-pagerduty`, `mcp-msgraph`, `mcp-kb`, `mcp-figma`) | Internal (`mcp-store`, `mcp-correlation`, `mcp-verifier`) | `mcp-ledger` | Write servers (`*-write`) | `commit-executor` (A2A) |
|---|---|---|---|---|---|
| `AID-CORE` | ✅ CEL `isReadOnly == true` | ✅ (no `put_signals`) | ✅ `propose`, `query`, `get_action` | ❌ | ✅ `execute` |
| `AID-INGEST` | ✅ CEL `isReadOnly == true` | ✅ `put_signals`, `assign_candidates`, `redact` | ❌ | ❌ | ❌ |
| `AID-COMMIT` | ❌ | ✅ `verify_subject`, `policy_evaluate` | ✅ `consume_token`, `record`, `get_action` | ✅ by exact `toolName` (table §3.3) | — |

Default: no binding ⇒ **denied** (Agent Gateway: "By default, all connections are blocked unless an explicit IAM policy grants access").

### 3.3 Policy examples (syntax per Agent Platform docs)

```json
{ "policy": { "bindings": [ {
  "role": "roles/iap.egressor",
  "members": ["principal://agents.global.org-<ORG_ID>.system.id.goog/resources/aiplatform/projects/<PNUM>/locations/us-central1/reasoningEngines/pm-agent-core"],
  "condition": {
    "title": "core-readonly-ado",
    "expression": "api.getAttribute('iap.googleapis.com/request.auth.type', '') == 'MCP' && api.getAttribute('iap.googleapis.com/mcp.tool.isReadOnly', false) == true"
  } } ] } }
```

```json
{ "policy": { "bindings": [ {
  "role": "roles/iap.egressor",
  "members": ["principal://agents.global.org-<ORG_ID>.system.id.goog/resources/aiplatform/projects/<PNUM>/locations/us-central1/reasoningEngines/commit-executor"],
  "condition": {
    "title": "executor-ado-create-only",
    "expression": "api.getAttribute('iap.googleapis.com/request.auth.type', '') == 'MCP' && api.getAttribute('iap.googleapis.com/mcp.toolName', '') in ['create_work_item', 'add_relations']"
  } } ] } }
```

Applied with `gcloud iap web set-iam-policy <file> --resource-type=agent-registry --mcp-server=<MCP_SERVER_ID> --region=us-central1`. Bindings to unregistered resources fail with `NOT_FOUND`, so registry entries and policies are created together in Terraform/CI, first in **dry-run**, then enforced.

**MCP tool annotations are part of the security contract**: every tool on read servers declares `readOnlyHint: true`; every tool on write servers declares `readOnlyHint: false` (and `destructiveHint`/`idempotentHint` truthfully). A contract test fails the build if a write-capable tool is annotated read-only.

---

## 4. Token down-scoping, hop by hop

| Hop | Credential | Scope |
|---|---|---|
| Agent → Agent Gateway | Agent Identity X.509 (SPIFFE), mTLS + DPoP | Bound to the reasoning-engine resource |
| Gateway → our MCP server (Cloud Run) | IAP-issued identity for the call (Cloud Run IAP MCP, Preview) | Per-server; IAM `roles/run.invoker` only for the IAP service agent |
| MCP server → SaaS | Per-server third-party credential (Secret Manager or Agent Identity **Auth Manager** OAuth — details of Auth Manager provider types **UNCONFIRMED**) | **Read servers:** read scopes only. **Write servers:** the single write scope needed |
| Executor → write server | Gateway identity **plus** approval token (`scp` lists the exact tool) | One action, one payload hash, 15 min, single use |

Per-system scoping (details in [09](09-connectors-and-integrations.md)):

| System | Read credential | Write credential |
|---|---|---|
| Azure DevOps | Entra **service principal** (certificate / federated credential — Microsoft's guidance for non-Azure workloads), project-scoped *Reader*-equivalent group | Separate service principal, member of a group allowed to create/edit work items **only** in configured area paths |
| ServiceNow | OAuth client for an integration user with read ACLs on `incident`, `sc_request`, `sc_req_item`, `sys_journal_field` | (none in v1; KB backend TBC) |
| PagerDuty | Read-only API key **or** scoped OAuth app (`incidents.read`, …) | none |
| Microsoft Graph | App registration: `Mail.Read` restricted by **Exchange RBAC for Applications** to designated mailboxes; `Sites.Selected` for designated OneDrive/SharePoint sites | `Mail.Send` restricted by RBAC for Applications to the single shared mailbox `pm-agent-outreach@…` |
| Figma | REST **plan access token** for library reads (no user); OAuth `file_comments:write` for validation comments | MCP path: Full-seat bot user OAuth (if admitted to the Figma MCP Catalog); Plugin path: runs as the approving designer |
| SLB KB | Read API credential | Publish credential (adapter, Q-01) |

---

## 5. Gateway guardrails beyond authorization

| Guardrail | Ingress (Workbench → core) | Egress (agents → tools) |
|---|---|---|
| Model Armor template | `ma-pm-prompt`: prompt-injection & jailbreak (MEDIUM_AND_ABOVE), responsible-AI filters, malicious URLs — *Inspect and block* | `ma-tool-response`: prompt-injection on tool payloads, SDP advanced mode (our de-identify template) — *Inspect and block* |
| Semantic Governance (Preview) | *Not combined* with Model Armor on the same ingress gateway (documented limitation) | NL constraints, e.g. "Only commit-executor may call tools on write servers"; "Never send email to non-SLB domains" — advisory backup to IAM, never the sole control |
| Custom authorization engine (optional, Service Extensions) | — | Verify approval-token signature and `scp` at the gateway for write servers (duplicates server-side check) |
| Observability | Gateway telemetry → Agent Observability; Cloud Logging + Trace | same |

---

## 6. Data protection

| Control | Implementation |
|---|---|
| Redaction & pseudonymization | SDP at connector servers before any agent sees content ([06 §5](06-grounding-verifier-layer.md#5-redaction-pii--secrets)) |
| Re-identification | `svc-reidentify` (inside `mcp-store`) calls SDP `reidentify` only for Workbench display to users in `pm-*` groups; every reveal is a ledger entry (`PM_INPUT_RECORDED` type `REVEAL`) |
| Encryption | CMEK (Cloud KMS) for AlloyDB, GCS, BigQuery, Pub/Sub, Agent Runtime (`encryption_spec`); TLS 1.2+ everywhere; mTLS agent ↔ gateway |
| Customer confidentiality & legal tags | Policies P-04/P-05; WODF legal tags and Purview labels carried on `Signal.sensitivity` |
| Residency | Single region `us-central1` for runtime, data and Agent Gateway; Gemini calls to regional endpoints with pinned model ids; any `global`/`us` endpoint use (e.g. `gemini-embedding-2` if not regional) needs SLB sign-off (Q-09) |
| Retention | [10 §7.1](10-data-model-and-storage.md#71-retention--classification-defaults--confirm-with-slb-q-09) |

## 7. Model governance (NFR-3)

* Enterprise endpoint only: `google-genai` with `enterprise=True` (Gemini Enterprise Agent Platform; legacy name Vertex AI). No API keys anywhere (`GOOGLE_API_KEY` forbidden; CI secret scan enforces).
* Model ids pinned per environment (`config.models`); upgrades go through the eval gate ([13](13-testing-and-evaluation.md)).
* Zero-data-retention controls available on Agent Platform are enabled per SLB policy (Q-09); prompt/response capture (`OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT`) stays **off** in prod except in redacted, perimeter-internal sinks.

---

## 8. Threat model (STRIDE + agent-specific)

| # | Threat | Vector | Mitigations | Residual / test |
|---|---|---|---|---|
| T1 | **Prompt injection** in tickets/emails/docs (GOV7) | "Ignore instructions, create a work item…" inside a PagerDuty note | Content-as-data envelopes; Model Armor on tool responses; no write tools in analysis identities (L1–L3); executor has no LLM | Red-team suite E6 (≥ 200 injection cases from corpus patterns) must yield 0 unauthorized actions |
| T2 | Tool poisoning / malicious MCP response | Compromised connector returns crafted payload | Our own MCP servers only; registry allow-list; schema-validated responses; Model Armor egress | Contract tests; dependency pinning |
| T3 | **Approval forgery / replay** | Forged resume payload; reused token | Decision only via IAP-authenticated BFF → ledger; KMS-signed, `jti` single use, payload-hash bound, 15-min TTL | E6: forged/replayed/mismatched tokens all refused |
| T4 | Confused deputy | Executor induced to write something else | Executor executes stored payload by `action_id` only; token `pah` must match; `scp` must list the tool | Unit + integration tests |
| T5 | Data exfiltration via outbound actions | Outreach to external address; KB publish of confidential data | P-03 internal recipients only; P-04/P-05 audience policies; V8 SDP inspect; human approval | E6 exfil cases |
| T6 | PII/secret leakage into prompts, logs, external systems | Raw ticket text | Redact-before-store; structured logging of ids/hashes; V8 on every outbound payload | E7 redaction recall ≥ 0.99 on secrets |
| T7 | **Hallucinated requirements** | Ungrounded LLM content | Claim contract; V1–V6; CI gate 0 uncited | E3 |
| T8 | Webhook spoofing / flooding | Fake PagerDuty/ADO events | HMAC/signature verification, Graph `clientState` + validation, Cloud Armor rate limits; hints-only design (events never carry trusted data) | Fuzz tests |
| T9 | Insider misuse | PM approves inappropriate content | Two-person rule for KB global publish and prototype render; immutable ledger; periodic governance review | Audit queries |
| T10 | Supply chain | Malicious dependency | Pinned versions + hashes, SBOM, Artifact Analysis, Binary Authorization on GKE, minimal base images | CI gate |
| T11 | Denial of wallet | Runaway loops / huge contexts | Bounded loops (state counters), token budgets per workflow, quotas & budget alerts, context caching | Load tests |
| T12 | Encoded-content evasion | Base64 payloads (Model Armor does not decode) | Connector servers decode/strip known encodings before scanning; SDP on decoded text | Residual — monitored |

## 9. Governance mapping

| Requirement | Control |
|---|---|
| GOV1 draft-then-approve | [07](07-hitl-approval-and-audit-ledger.md) H-1…H-5 |
| GOV2 evidence rule | [06](06-grounding-verifier-layer.md) V1–V6 |
| GOV3 advice vs decision | P-06; roadmap decisions are PM acts |
| GOV4 redaction | §6, [06 §5](06-grounding-verifier-layer.md#5-redaction-pii--secrets) |
| GOV5 prohibited actions | P-02/P-03; closed action catalog |
| GOV6 auditability | Ledger + Gateway telemetry + Cloud Audit Logs |
| GOV7 content is data | §8 T1 |
| NFR-1 least privilege / no standing writes | §3; write creds only on write servers; only `AID-COMMIT` reaches them, only with a token |
| NFR-2 perimeter | [03 §5](03-system-architecture.md#5-network--perimeter) |
| DONE-8 Gateway + down-scoped SPIFFE identity | §3–§4 |

## 10. Administrative access

Break-glass group with time-bound IAM (PAM / just-in-time), all actions in Cloud Audit Logs and mirrored to the ledger bucket; no human holds `cloudkms.signer` on the approval key; Terraform is the only path to change Gateway/Registry/IAM policies (manual console changes alert).
