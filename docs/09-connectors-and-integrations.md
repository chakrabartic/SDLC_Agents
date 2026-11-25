# 09 — Connectors & Integrations

> Every external system is reached through an MCP server we own (Cloud Run), split into **read** and **write** servers with separately-scoped credentials. Labels: **[V]** verified against official docs downloaded 2026-10-12 · **[K]** vendor-documented behaviour from prior knowledge, to be confirmed in the M1 connector spike · **UNCONFIRMED** open item (tracked in [15](15-risks-assumptions-open-questions.md)).

---

## 1. Connector contract (applies to every MCP server)

| Aspect | Contract |
|---|---|
| Transport | MCP Streamable HTTP on Cloud Run (`--functional-type=mcp-server`, IAP-protected — Preview [V]); registered in Agent Registry; reachable only via Agent Gateway egress |
| Tool naming | `<system>.<verb>_<object>` in docs; MCP tool names are the bare verb_object (`list_incidents`) because Gateway CEL matches `mcp.toolName` per server |
| Annotations | Truthful `readOnlyHint` / `destructiveHint` / `idempotentHint` / `openWorldHint` — they drive Gateway CEL (`mcp.tool.isReadOnly`) and are contract-tested |
| Output | Canonical `Signal` / search hits per [10](10-data-model-and-storage.md); **redacted server-side** (SDP) before return; permalinks mandatory; large blobs as `ArtifactHandle` (IR4) |
| Pagination | Opaque `cursor` in/out; server enforces page caps |
| Change detection | Delta cursors persisted in `mcp-store` (`config.connector_cursors`); webhooks only produce *hints* |
| Rate limits | Token-bucket per credential + honor `Retry-After` / vendor headers; exponential backoff with jitter; 429s surface as `retryable` |
| Errors | Typed: `unavailable`, `auth_failed`, `rate_limited`, `not_found`, `validation_failed` — never empty successes (IR3) |
| Write servers | Require `approval_token` argument; verify JWS (`aud`, `exp`, `scp` ∋ tool, `pah` == hash(payload)) **before** any outbound call; support `validate_only` where the vendor does |
| Tests | Recorded-fixture contract tests (no network in unit tests); live smoke tests against SLB sandboxes in stage only |

### 1.1 Logical tool catalog (requirements §11) → MCP tools

| Logical tool | MCP tools | Server | Class |
|---|---|---|---|
| `signals.fetch` | `list_changed_work_items`, `get_work_items`, `list_comments` · `list_records`, `list_journal` · `list_incidents`, `list_notes`, `list_log_entries` · `mail_delta`, `get_message`, `drive_delta`, `get_item_text`, `read_workbook` | `mcp-ado`, `mcp-servicenow`, `mcp-pagerduty`, `mcp-msgraph` | Read |
| `problem.correlate` | `find_candidates`, `assign_candidates`, `related` | `mcp-correlation` | Compute |
| `priorart.search` | `search_work_items` · `search_articles` · `evidence_search` | `mcp-ado`, `mcp-kb`, `mcp-store` | Read |
| `requirement.gap_analyze` / `requirement.draft` / `architecture.precheck` | (LLM nodes) + `save_draft`, `verify_subject` | `mcp-store`, `mcp-verifier` | Compute / Draft |
| `ado.create_requirement` | `create_work_item`, `add_relations` | `mcp-ado-write` | **Write (HITL)** |
| `prototype.generate` | `search_dls`, `get_component`, `get_file_nodes`, `render_images` + `save_draft` | `mcp-figma`, `mcp-store` | Read / Draft |
| `prototype.publish` | render (executor) · `post_comment` · `PROTOTYPE_RELEASE_INTERNAL` | `svc-prototype-renderer`, `mcp-figma-write`, `mcp-ledger` | **Write (HITL)** |
| `knowledge.draft` | (LLM node) + `search_articles`, `near_duplicates` | `mcp-kb`, `mcp-verifier` | Draft |
| `knowledge.publish` | `publish_article` | `mcp-kb-write` | **Write (HITL)** |
| `roadmap.trace` | `trace_query`, `trace_propose_links`, `run_named_query` | `mcp-store` | Compute |
| `outreach.draft` / send | (LLM node) · `send_mail` | `mcp-msgraph-write` | Draft / **Write (HITL)** |

---

## 2. Azure DevOps (`mcp-ado`, `mcp-ado-write`)

| Topic | Design |
|---|---|
| API | REST **7.1** [V] (7.2 preview exists [K]) — org/project from config (corpus shows org `slb1-swt`, project `Planck`) |
| Reads | WIQL `POST …/_apis/wit/wiql?api-version=7.1` (ids only, 20k cap [K]) → `POST …/_apis/wit/workitemsbatch` (≤ 200 ids, `$expand=Relations` [K]); comments `GET …/workItems/{id}/comments` (**7.1-preview.4** [K]); backlogs `…/{team}/_apis/work/backlogs` [K]; changed items via WIQL on `System.ChangedDate` > cursor |
| Create | `POST https://dev.azure.com/{org}/{project}/_apis/wit/workitems/${type}?api-version=7.1`, `Content-Type: application/json-patch+json`, query `validateOnly`, `bypassRules`, `suppressNotifications`, `$expand` [V]. **Pre-flight = `validateOnly=true`**; `bypassRules` is **never** used |
| Update / links | `PATCH …/workitems/{id}` with `{"op":"test","path":"/rev"}` guard and `/relations/-` (`System.LinkTypes.Related`, `Hyperlink`) [K] |
| Auth | Microsoft Entra ID; service principal for non-Azure workloads with certificate/federated credentials; PATs discouraged ("use sparingly") [V]. Separate SPs for read and write |
| Limits | 200 TSTUs / sliding 5 min per identity; delays then 429 (`TF400733`); headers `Retry-After`, `X-RateLimit-Resource/-Delay/-Limit/-Remaining/-Reset` (+ `-Cost`) [V] |
| Change hints | Service hooks: `workitem.created/updated/commented` → `signal-event-gateway` (basic-auth/secret header) [K] |
| Signal mapping | `source_record_id`=id, `source_version`=rev, `permalink`=`https://dev.azure.com/{org}/{project}/_workitems/edit/{id}`, comments → child signals (`parent_signal_id`) |
| Alternative | Microsoft **remote ADO MCP server (preview)** `https://mcp.dev.azure.com/{organization}` [V-README]; auth model for non-interactive service principals **UNCONFIRMED** — candidate replacement for `mcp-ado` *reads* only; writes stay on `mcp-ado-write` (token verification) |

Template field mapping and work item types ("Requirement", "Initiative") follow `config.ado_field_map` (SLB process template — Q-04).

## 3. ServiceNow (`mcp-servicenow`)

| Topic | Design |
|---|---|
| Reads | Table API `GET /api/now/table/{incident|sc_request|sc_req_item}` with `sysparm_query` (`sys_updated_on>cursor^ORDERBYsys_updated_on`), `sysparm_fields`, `sysparm_limit/offset`, `sysparm_display_value=all` [K]; journal: `sys_journal_field` filtered by `element_id` and `element IN comments,work_notes` [K]; CSM cases (`sn_customerservice_case`, seen in corpus as `CS2461006`) if in scope (Q-05) |
| Auth | OAuth 2.0 (Application Registry; `oauth_token.do`) for a read-only integration user with ACLs on the above tables [K] |
| Limits | Instance-defined inbound REST rate-limit rules → 429 [K] |
| Hints | Outbound REST message / Business Rule on update → `signal-event-gateway` (shared-secret HMAC) [K] |
| Corpus note | Integration notes copy PagerDuty/ADO links into incidents (e.g. "Case CS2461006 associated", ADO URL) → strong `CrossRef` hard links |
| KB | If SLB KB = ServiceNow Knowledge: read via `/api/sn_km_api/knowledge/articles` [K]; writes via `kb_knowledge` draft + KB publish workflow [K]; KM API write capability **UNCONFIRMED** |

## 4. PagerDuty (`mcp-pagerduty`)

| Topic | Design |
|---|---|
| Reads | REST v2 `GET /incidents` (`since/until`, `statuses[]`, `service_ids[]`, `limit ≤ 100`), `GET /incidents/{id}/notes`, `GET /incidents/{id}/log_entries` [K] |
| Auth | Read-only REST API key or scoped OAuth app (`incidents.read`, …) [K] |
| Limits | Per-key/user per-minute limit → 429 (number **UNCONFIRMED**) |
| Hints | Webhooks v3 (`incident.triggered/acknowledged/resolved/annotated`, …) signed `X-PagerDuty-Signature` (HMAC-SHA256) [K] |
| MCP note | The local `pagerduty-mcp-server` repo is **deprecated/read-only**; PagerDuty now offers a hosted remote MCP server with OAuth [V-README] (endpoint/scopes UNCONFIRMED) — we keep our own read server for normalization and provenance |
| Mapping | Incident → `incident`; notes and log entries → child `journal_note` signals; permalink = incident HTML URL |

## 5. Microsoft Graph (`mcp-msgraph`, `mcp-msgraph-write`)

| Topic | Design |
|---|---|
| Mail (read) | `GET /users/{id}/mailFolders/{folder}/messages/delta` per folder; threads by `conversationId`; `Prefer: odata.maxpagesize` [K]; mailboxes = designated PM/product shared mailboxes only |
| OneDrive / SharePoint | `GET /drives/{drive}/root/delta`; `GET /drives/{d}/items/{i}/content` (convert/parse docx/pptx/pdf/xlsx server-side); search `…/root/search(q='…')` [K] |
| Trackers (Excel) | Workbook API (`…/workbook/worksheets/{n}/range(address='…')`, tables) — **app-only support UNCONFIRMED** ⇒ fallback: download `.xlsx` and parse rows (each row → `tracker_row` signal with cell-range permalink) |
| Permissions | Application `Mail.Read` scoped via **Exchange RBAC for Applications** (management scope = allowed mailboxes; do **not** also grant tenant-wide Mail.Read); `Sites.Selected` granted per site [K] |
| Write (approval only) | `POST /users/{shared-mailbox}/sendMail` with `Mail.Send` scoped by RBAC to that mailbox; recipients restricted to SLB domains (P-03) [K] |
| Change notifications | `POST /subscriptions` for mail folders and drive roots; endpoint echoes `validationToken` within 10 s; `clientState` check; lifecycle notifications (`reauthorizationRequired`, `missed`) handled by renewal job [K] |
| Limits | 429 + `Retry-After`; Outlook per-mailbox concurrency limits; `$batch` ≤ 20 [K] |
| Sensitivity | `extractSensitivityLabels` on drive items → `Signal.sensitivity.purview_label` [K] |
| Extension | Teams channel messages via Graph (same server, extension flag) |

## 6. Figma (`mcp-figma`, `mcp-figma-write`, `dls-catalog-sync`, `svc-prototype-renderer`)

| Topic | Verified facts | Design |
|---|---|---|
| REST capability | Official OpenAPI spec (v0.44.0) has **no endpoint to create files, frames, nodes or instances**; writes limited to comments/reactions, webhooks, variables (Enterprise), dev resources [V] | REST used for **reading** the DLS library, rendering screenshots, posting validation comments, webhooks |
| Library reads | `GET /v1/teams/{id}/components|component_sets|styles`, `GET /v1/files/{key}/components…`, `GET /v1/components/{key}` [V]; user-less **plan access tokens** work for these reads [V] | `dls-catalog-sync` CronJob snapshots the SLB DLS (keys, variant props, descriptions, publish version) into `config.dls_catalog` |
| Variables | `GET/POST /v1/files/{key}/variables` — Enterprise only [V] | Read tokens/variables for spec composition when SLB is on Enterprise (Q-06) |
| Images | `GET /v1/images/{key}?ids=…` [V] | Screenshots for validation packages |
| Comments | `POST /v1/files/{key}/comments` (OAuth `file_comments:write`) [V] | `FIGMA_POST_COMMENT` (approval) |
| Webhooks v2 | `FILE_COMMENT`, `FILE_VERSION_UPDATE`, `LIBRARY_PUBLISH`, … (context team/project/file) [V] | Feedback loop (FR-B5); catalog refresh on `LIBRARY_PUBLISH` |
| Remote MCP write | `use_figma` (create/edit pages, frames, components, variants, variables via Plugin API JS in file context), `search_design_system`, `get_libraries`, `create_new_file` [V] | **Path B** renderer once admitted |
| MCP constraints | Only clients in the **Figma MCP Catalog** may connect (waitlist for new clients); per-user OAuth; **Full seat** required to write; limits: 20 kb output per call, no image assets, **no custom fonts**; Enterprise read-tool limits 600/day, 20/min [V] | Hence Path A (private plugin) is primary — ADR-006 |
| Plugin API | `figma.importComponentByKeyAsync`, `createInstance`, `setProperties`, `createFrame`… [K] | **Path A**: private SLB org plugin executes the compiled program fetched (token-gated) from `svc-prototype-renderer` |

## 7. SLB Knowledge Base (`mcp-kb`, `mcp-kb-write`) — adapter

No public SLB KB API exists in the corpus or public docs (**UNCONFIRMED** — Q-01; the pain-point doc refers to the "SLB knowledge management system"). The servers implement a backend-agnostic adapter:

```text
KbAdapter
  capabilities() -> {native_draft, approval_workflow, versioning, labels}
  search(query, filters, top) -> [KbHit]            # read server
  get(article_id) -> KbArticle                       # read server
  validate(draft) -> ValidationResult                # write server (pre-flight)
  create_draft(draft) -> ExternalRef                 # write server
  publish(ref) -> ExternalRef | PublishRequiresWorkflow
  retire(ref)
```

| Candidate backend | Draft/publish path [K] |
|---|---|
| ServiceNow Knowledge | `kb_knowledge` record (`workflow_state=draft`) via Table API → KB publish workflow |
| Confluence Cloud v2 | `POST /wiki/api/v2/pages` (`status=draft`) → `PUT …/pages/{id}` (`status=current`, version+1) |
| SharePoint pages (Graph) | `POST /sites/{id}/pages` → `…/microsoft.graph.sitePage/publish` |
| ADO Wiki | `PUT …/_apis/wiki/wikis/{wiki}/pages?path=…` (If-Match for updates) |

If the backend has its own approval workflow, `KB_PUBLISH_ARTICLE` performs `create_draft` + submit, and the ledger records the backend's publish event when observed (both approvals are captured).

## 8. Google Gen AI SDK (all model calls)

```python
# Verified signature (google-genai 2.29.0; pinned 2.34.0 — the newest 2.x, since ADK 2.15 requires <3):
#   Client(*, enterprise=None, vertexai=None, api_key=None,
#   credentials=None, project=None, location=None, debug_config=None, http_options=None)
from google import genai
client = genai.Client(enterprise=True, project="chayan-genai-bb", location="us-central1")
```

| Setting | Value |
|---|---|
| Env (local dev) | `GOOGLE_GENAI_USE_VERTEXAI=true` (workspace rule) **and** `GOOGLE_GENAI_USE_ENTERPRISE=true` (current name; ADK docs: equivalent), `GOOGLE_CLOUD_PROJECT`, `GOOGLE_CLOUD_LOCATION=us-central1`. On Agent Runtime, project/location are platform-provided (do not set them as env vars [V]) |
| Forbidden | `GOOGLE_API_KEY`, `google.generativeai` (CI lint rule) |
| Models | Pinned ids (ADR-008); no `*-latest` aliases on regional endpoints [V] |
| Structured output | `response_schema` / ADK `output_schema` on every drafting node |
| Safety | `model_armor_config` (Agent-Platform-only field [V]) on direct verifier-judge calls; ADK `ModelArmorPlugin` for agents |
| Tests | `DebugConfig(client_mode="replay")` record/replay (`GOOGLE_GENAI_CLIENT_MODE`, `GOOGLE_GENAI_REPLAYS_DIRECTORY`) for deterministic LLM tests [V] |

## 9. Webhook ingress (`signal-event-gateway`, GKE)

| Vendor | Verification | Hint published |
|---|---|---|
| ADO service hooks | Shared secret header / basic auth over TLS; source IP ranges via Cloud Armor | `{system:"ado", record_id, rev}` |
| ServiceNow outbound REST | HMAC-SHA256 shared secret | `{system:"servicenow", table, sys_id}` |
| PagerDuty Webhooks v3 | `X-PagerDuty-Signature` HMAC | `{system:"pagerduty", incident_id, event_type}` |
| Microsoft Graph | `validationToken` echo; `clientState` match; optional encrypted rich notifications not used | `{system:"msgraph", resource, change_type}` |
| Figma webhooks v2 | Passcode verification | `{system:"figma", file_key, event_type}` |

Hints carry identifiers only; the ingestion workflow always re-reads the record through the read MCP server (so a spoofed hint can at worst cause a redundant read).
