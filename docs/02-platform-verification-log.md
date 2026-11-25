# 02 — Platform Verification Log

> Requirement: *"Verify current ADK 2.x and A2UI APIs against official Google docs before designing. Flag anything you can't confirm rather than guessing."*
>
> **Method (2026-10-12).** Official pages were downloaded directly from vendor domains (adk.dev, docs.cloud.google.com, github.com/a2ui-project, github.com/googleapis, pypi.org, developers.figma.com, learn.microsoft.com, github.com/microsoft) into a local, git-ignored cache (`.research_cache/`), plus the Gemini API docs MCP index of the official SDK sources. The web-search tool was non-functional during this session, so only *directly fetched* pages count as evidence.
>
> **Status labels:** ✅ VERIFIED (read in an official source) · 🟡 PARTIAL (verified with caveats/conflicts) · ❓ UNCONFIRMED (not found in fetched official sources — design does not depend on it, or a spike is planned).

---

## 1. Verified stack (pin these — updated 2026-11-24, see §4)

| Component | Version / id | Released | Source |
|---|---|---|---|
| `google-adk` (Python) | **2.15.0** (2.x GA since 2026-05-19); Python ≥ **3.11**; needs `google-genai >=2.34,<3` | 2026-11-19 | https://pypi.org/project/google-adk/ · https://adk.dev/2.0/ |
| A2UI protocol | **v1.0** — spec status **"Current"** (was RC on 2026-10-12); v0.9.1 superseded | GA 2026-11-11 | https://github.com/a2ui-project/a2ui (README, `specification/v1_0/`) |
| A2UI Python SDK | `a2ui-agent-sdk` **0.12.1** (+ `a2ui-core` 0.7.x) · `a2ui-adk` **0.3.0** · `a2ui-a2a` **0.4.0** (pins `a2a-sdk>=0.3.0,<0.4.0`) | 2026-11-10…23 | https://pypi.org/project/a2ui-agent-sdk/ · /a2ui-adk/ · /a2ui-a2a/ |
| `a2a-sdk` | latest 1.3.1, **but use 0.3.26** for `a2ui-a2a` compatibility | 2026-04-09 (0.3.26) | https://pypi.org/project/a2a-sdk/ |
| `google-genai` | **2.34.0** (newest 2.x); 3.6.1 exists but is **outside ADK 2.15's `<3` range**; `Client(enterprise=True, project, location)` | 2026-10-29 | https://pypi.org/project/google-genai/ · client.py |
| `mcp` (Python SDK) | **2.3.0** — `FastMCP` renamed **`MCPServer`** (`mcp.server.mcpserver`) | 2026-10-02 | installed-package introspection |
| Gemini (Agent Platform) | `gemini-3.8-flash` (GA 2026-09-02), `gemini-3.5-flash-lite` (GA), `gemini-embedding-2` (GA); `gemini-3.1-pro-preview` (Preview) — **not re-checked on 2026-11-24** (configurable placeholder `config.models`) | — | https://docs.cloud.google.com/gemini-enterprise-agent-platform/models/google-models |

---

## 2. Findings

### 2.1 ADK 2.x (Python)

| # | Finding | Status | Source | Design impact |
|---|---|---|---|---|
| A1 | ADK 2.0 introduces the **Workflow Runtime** (graph engine); Agents/Tools/Functions are *nodes*; `BaseAgent` now subclasses `BaseNode` | ✅ | https://adk.dev/2.0/ | Orchestration built on `Workflow` / `@node` ([05](05-orchestration-and-workflows.md)) |
| A2 | Breaking: custom `_run_async_impl()` overrides are silently bypassed; don't append to `session.events`; framework catches exceptions for retries/HITL — never catch `BaseException` (`NodeInterruptedError`) | ✅ | https://adk.dev/2.0/ | Engineering rules + CI lint ([05 §6](05-orchestration-and-workflows.md#6-adk-2x-engineering-rules-from-the-20-migration-notes)) |
| A3 | Imports: `from google.adk import Agent, Workflow, Event, Context, Runner`; `from google.adk.workflow import START, node, JoinNode`; `from google.adk.events import RequestInput`; `from google.adk.apps import App, ResumabilityConfig` | ✅ | https://adk.dev/graphs/ · /graphs/human-input/ · /apps/ | Code sketches use these |
| A4 | Graph syntax `Workflow(name, edges=[("START", a, b), (router, {"ROUTE": node})])`; routes via `Event(route=…)`; `JoinNode` fan-in; loops are back-edges, **not auto-bounded** | ✅ | https://adk.dev/graphs/routes/ · /graphs/data-handling/ | Explicit attempt counters |
| A5 | Dynamic workflows: `@node(rerun_on_resume=True)` + `await ctx.run_node(...)`; automatic checkpointing of completed children | ✅ | https://adk.dev/graphs/dynamic/ | Supervisor + requirement workflow |
| A6 | Collaboration modes `Agent(mode="chat"|"task"|"single_turn")`; task mode **disabled inside graphs in 2.0** (re-enablement not confirmed) | 🟡 | https://adk.dev/workflows/collaboration/ | LLM nodes use `single_turn` |
| A7 | HITL: `yield RequestInput(message, payload, response_schema)`; surfaced as synthetic call `adk_request_input`; resume via `ResumabilityConfig(is_resumable=True)` + `invocation_id` | ✅ | https://adk.dev/graphs/human-input/ · /runtime/resume/ | Approval pauses |
| A8 | **Exact Python FunctionResponse payload to answer `RequestInput`** | ❓ | — | Contract test in M1; design re-reads ledger anyway |
| A9 | Tool confirmation (`require_confirmation`, `request_confirmation`) is experimental and **not supported with `VertexAiSessionService` / `DatabaseSessionService`** | ✅ | https://adk.dev/tools-custom/confirmation/ | Not used for commits |
| A10 | Callback keyword signatures (`callback_context`, `llm_request`, `llm_response`, `tool`, `args`, `tool_context`); plugins via `BasePlugin`, keyword-only hooks, registered on `App(plugins=[…])` (`Runner(plugins=…)` deprecated) | ✅ | https://adk.dev/callbacks/types-of-callbacks/ · /plugins/ | `GroundingGuardPlugin` |
| A11 | Built-in `ModelArmorPlugin` (`google.adk.integrations.model_armor`, ADK ≥ 2.8, `google-adk[gcp]`), `BigQueryAgentAnalyticsPlugin`, `ReflectAndRetryToolPlugin`, `LoggingPlugin`, `ToolCallIntegrityPlugin` (2.11) | ✅ | https://adk.dev/plugins/ · CHANGELOG | Plugin stack |
| A12 | `VertexAiSessionService`, `VertexAiMemoryBankService`, `GcsArtifactService`; state prefixes `app:`/`user:`/`temp:` | ✅ | https://adk.dev/sessions/ | [05 §7](05-orchestration-and-workflows.md#7-state-memory-and-context) |
| A13 | `McpToolset(connection_params=StreamableHTTPConnectionParams(url, headers, timeout), tool_filter=[…])`; GCP Agent Identity auth providers for MCP | ✅ | https://adk.dev/ (llms-full) | Connector clients |
| A14 | A2A: `to_a2a(...)`, `RemoteA2aAgent(..., use_legacy=False)`, `pip install google-adk[a2a]` (a2a-sdk 0.3.x or 1.x auto-detected) | ✅ | https://adk.dev/a2a/ | Core → executor |
| A15 | Eval: `adk eval`, `AgentEvaluator.evaluate`, `.test.json`/`.evalset.json`, metrics incl. `hallucinations_v1`, `safety_v1`, `tool_trajectory_avg_score`, rubric and multi-turn metrics | ✅ | https://adk.dev/evaluate/criteria/ | [13](13-testing-and-evaluation.md) |
| A16 | `adk eval` with a `Workflow` root agent | ❓ | — | Fallback harness planned |
| A17 | Deploy: `adk deploy cloud_run`, `adk deploy gke`, `adk deploy agent_engine` (legacy name kept for Agent Runtime); Agents CLI (`google-agents-cli`) | ✅ | https://adk.dev/deploy/ | [12](12-deployment-and-operations.md) |
| A18 | Env var `GOOGLE_GENAI_USE_ENTERPRISE` "was previously `GOOGLE_GENAI_USE_VERTEXAI`… equivalent"; `gemini-flash-latest` alias **may not work on regional endpoints (us-central1)** → pin ids | ✅ | https://adk.dev/agents/models/google-gemini/ | Set both env vars; pinned models |
| A19 | ADK docs moved to **adk.dev** (PyPI still links google.github.io/adk-docs); some pages (custom agents, resume) still teach 1.x patterns | 🟡 | https://adk.dev/ | Follow 2.0 migration page where they conflict |

### 2.2 A2UI

| # | Finding | Status | Source | Design impact |
|---|---|---|---|---|
| U1 | Repo is `github.com/a2ui-project/a2ui`; status "early stage public preview" | ✅ | README | Adapter isolation + contract tests |
| U2 | v0.9.1 messages: server→client `createSurface`, `updateComponents`, `updateDataModel`, `deleteSurface` (+ required `version`); client→server `action`, `error` | ✅ | `specification/v0_9_1/json/*.json` | [11](11-a2ui-pm-workbench.md) |
| U3 | A2A extension URI `https://a2ui.org/a2a-extension/a2ui/v0.9.1`; DataPart `metadata.mimeType="application/a2ui+json"` (v0.9.1; old `application/json+a2ui` deprecated); activation header `X-A2A-Extensions` | ✅ | `a2ui_extension_specification.md`, `evolution_guide.md` | Transport |
| U4 | SDK `create_a2ui_part` emits the **deprecated** MIME unless `version="0.9.1"` is passed | ✅ | `python/a2ui_agent/src/a2ui/a2a/parts.py` | Always pass version |
| U5 | Basic catalog = 18 components (no `Table`, `Heading`, `ConditionList`); custom catalogs are the intended production path (`$id == catalogId`, additive versioning) | ✅ | `docs/public/concepts/catalogs.md`, protocol spec | SLB custom catalog |
| U6 | Client capabilities must be **version-keyed** (`{"v0.9.1": {...}}`); SDK rejects flat shape | ✅ | SDK CHANGELOG | BFF metadata |
| U7 | ADK emission patterns: tagged text (`<a2ui-json>`) or `SendA2uiToClientToolset` (experimental); `A2uiPartConverter` / `A2uiEventConverter`; `A2uiSubagentMap` (strip unowned surfaces — mandatory) | ✅ | SDK `adk/` sources, sample | Deterministic decision surfaces |
| U8 | Transports: A2A ✅ stable, AG-UI ✅ stable, REST planned, SSE/WebSocket proposed | ✅ | `concepts/transports.md` | A2A streaming |
| U9 | Renderers React/Lit/Angular/Flutter stable for v0.8 & v0.9.1, v1.0 planned; npm `@a2ui/angular` confirmed; `@a2ui/lit` / `@a2ui/react` names | 🟡 | `reference/renderers.md` | Angular renderer |
| U10 | SDK main-branch docs reference **unreleased** APIs; ADK's A2UI page is **stale** (removed `A2uiSchemaManager`, legacy MIME) | ✅ | SDK CHANGELOG vs docs | Code to 0.8.0 surface |
| U11 | `onUiComponentDelivery` JSON-RPC frames (workspace notes) | ❌ not A2UI | zero occurrences in spec/SDK/ADK docs | Replaced by official messages |
| U12 | `a2a.types` → `a2a.compat.v0_3.types` monkeypatch (workspace notes) | ❓ no official source | — | Not used; pin `a2a-sdk` 0.3.x instead |

### 2.3 Google Gen AI SDK & models

| # | Finding | Status | Source |
|---|---|---|---|
| G1 | `genai.Client(*, enterprise, vertexai, api_key, credentials, project, location, debug_config, http_options)`; `enterprise` = Gemini Enterprise Agent Platform endpoints (*previously Vertex AI API*); `vertexai` is the legacy flag; conflicting values ⇒ `ValueError` | ✅ | https://github.com/googleapis/python-genai/blob/main/google/genai/client.py |
| G2 | Env `GOOGLE_GENAI_USE_ENTERPRISE=true` documented; legacy `GOOGLE_GENAI_USE_VERTEXAI` still honored per ADK docs (equivalent) | 🟡 | client.py docstring; adk.dev |
| G3 | `GenerateContentConfig.model_armor_config` (Agent-Platform only) | ✅ | python-genai `models.py` |
| G4 | Record/replay testing via `DebugConfig` (`GOOGLE_GENAI_CLIENT_MODE`, `GOOGLE_GENAI_REPLAYS_DIRECTORY`) | ✅ | client.py |
| G5 | Interactions API on Agent Platform: Preview, `global` endpoint only | ✅ | https://ai.google.dev/gemini-api/docs/migrate-to-cloud | ADK uses model calls on `us-central1`; Interactions not required |
| G6 | Per-model availability on `us-central1` vs `global` for 3.8 Flash / 3.5 Flash-Lite / `gemini-embedding-2` | ❓ | Memory Bank docs: `gemini-embedding-2` only via `global`/`us`/`eu` | M0 spike; fallback `us` multi-region (residency sign-off Q-09) |
| G7 | `gemini-3-pro-preview` was shut down 2026-03-09; 2.x models legacy/limited | ✅ | https://ai.google.dev/gemini-api/docs/deprecations | Not used |

### 2.4 Gemini Enterprise Agent Platform (Agent Runtime, Sessions, Memory Bank)

| # | Finding | Status | Source |
|---|---|---|---|
| P1 | "Vertex AI Agent Engine" → **Agent Runtime on Gemini Enterprise Agent Platform**; Sessions/Memory Bank renamed; resource name still `reasoningEngines` | ✅ | https://docs.cloud.google.com/gemini-enterprise-agent-platform/vertex-ai-name-changes |
| P2 | Deploy via `client.runtimes.create(agent=…, config={identity_type, service_account, psc_interface_config, encryption_spec, min/max_instances, resource_limits, container_concurrency, agent_framework, env_vars})`; don't set `GOOGLE_CLOUD_PROJECT/LOCATION` env vars | ✅ | …/scale/runtime/deploy-an-agent |
| P3 | `google-cloud-agentplatform` 2.0.1 (2026-09-18) splits agent APIs from `google-cloud-aiplatform` (breaking) — exact client construction for `runtimes` | ❓ | …/release-notes | M0 spike |
| P4 | us-central1 supports Runtime, Sessions, Memory Bank, Sandboxes, **Agent Gateway** | ✅ | …/resources/agent-locations |
| P5 | Long-running operations up to 7 days; revisions & traffic splitting (Preview) | ✅ | …/release-notes |

### 2.5 Agent Gateway, Agent Identity, Registry, policies

| # | Finding | Status | Source |
|---|---|---|---|
| S1 | Agent Gateway = enforcement point for user↔agent, agent↔tool, agent↔agent; modes **Client-to-Agent (ingress, Agent Runtime only)** and **Agent-to-Anywhere (egress, Agent Runtime + Gemini Enterprise)**; GA 2026-06-18; `gcloud network-services agent-gateways`, REST `agentGateways` | ✅ | https://docs.cloud.google.com/gemini-enterprise-agent-platform/govern/gateways/agent-gateway-overview |
| S2 | Enforcement: IAM Unified Access Policies + PAB (via IAP, `iap.resources.egressViaIAP`), Agent Registry check, Model Armor, Semantic Governance, custom authz (Service Extensions); deny by default | ✅ | same |
| S3 | "Envoy-based" | ❓ | not stated in docs | No dependency |
| S4 | MCP traffic is parsed for attributes → tool-level CEL (`iap.googleapis.com/mcp.toolName`, `iap.googleapis.com/mcp.tool.isReadOnly`, `request.auth.type == 'MCP'`); role `roles/iap.egressor`; `gcloud iap web set-iam-policy … --resource-type=agent-registry --mcp-server=…` | ✅ | …/govern/policies/assign-identity-iam |
| S5 | Agent Identity: SPIFFE ID per agent, mTLS + DPoP; principal formats for Agent Runtime, Gemini Enterprise, **Cloud Run (Agent Identity)**, WIF; GA; `agentidentity.googleapis.com` API Preview | ✅ | same + https://docs.cloud.google.com/iam/docs/managed-workload-identity |
| S6 | Cloud Run / GKE workloads as **governed sources** through Agent Gateway (policies accept their principals, but routing is not documented) | ❓ | conflict between overview and IAM pages | Agents placed on Agent Runtime; Cloud Run/GKE used as destinations |
| S7 | Cloud Run as IAP-protected MCP destination (`--iap --functional-type=mcp-server`) | 🟡 Preview | assign-identity-iam | MCP servers on Cloud Run |
| S8 | Semantic Governance: Preview; requires `identity_type=AGENT_IDENTITY` + `agent_gateway_config` at deploy (immutable) | ✅ | …/configure-semantic-governance | Secondary control only |
| S9 | VPC-SC enforced for gateways created after 2026-09-08 with connectivity template `ALL_TRAFFIC` | ✅ | release notes | Day-one config |
| S10 | Agent Identity **Auth Manager** OAuth specifics for third-party APIs (ADO/Graph/Figma) | ❓ | only "simplifies OAuth 2.0 handshakes" stated | Secret Manager fallback |

### 2.6 Model Armor, SDP, VPC-SC

| # | Finding | Status | Source |
|---|---|---|---|
| M1 | `modelarmor.googleapis.com`; templates; filters: RAI, prompt injection & jailbreak, SDP basic/advanced, malicious URLs (first 256); inspect-only vs inspect-and-block; encoded content not decoded | ✅ | https://docs.cloud.google.com/model-armor/overview |
| M2 | SDP `SECURITY_DATA` infoTypes incl. `AUTH_TOKEN`, `AZURE_AUTH_TOKEN`, `GCP_API_KEY`, `JSON_WEB_TOKEN`, `PASSWORD`, `OAUTH_CLIENT_SECRET`, … ; no ADO/GitHub PAT infoType | ✅ | https://docs.cloud.google.com/sensitive-data-protection/docs/infotypes-reference |
| M3 | VPC-SC supports Agent Platform, Agent Runtime, Agent Registry (GA); Cloud Run/GKE/Model Armor rows not extracted in this pass | 🟡 | https://docs.cloud.google.com/vpc-service-controls/docs/supported-products |

### 2.7 Figma

| # | Finding | Status | Source |
|---|---|---|---|
| F1 | REST API (spec v0.44.0, 54 operations) **cannot create files/frames/nodes/instances**; writes only comments/reactions, webhooks, variables (Enterprise), dev resources | ✅ | https://github.com/figma/rest-api-spec · https://developers.figma.com/llms.txt |
| F2 | Remote MCP server has write tools (`use_figma`, `create_new_file`, `generate_figma_design`, `upload_assets`) and design-system tools (`search_design_system`, `get_libraries`) | ✅ | https://developers.figma.com/docs/figma-mcp-server/tools-and-prompts/ |
| F3 | **Only clients in the Figma MCP Catalog can connect** (waitlist for new clients); per-user OAuth; Full seat needed to write; `use_figma` limits: 20 kb output/call, no image assets, **no custom fonts** | ✅ | …/rate-limits-access/ · …/write-to-canvas/ |
| F4 | Plugin API canvas creation (`importComponentByKeyAsync`, `createInstance`…) | 🟡 prior knowledge | https://developers.figma.com/docs/plugins/ | Spike in M6 |
| F5 | Plan access tokens (no user) for library reads | ✅ | rest-api-spec | `dls-catalog-sync` |

### 2.8 Enterprise connectors

| # | Finding | Status | Source |
|---|---|---|---|
| C1 | ADO Work Items – Create 7.1 with `validateOnly`, `bypassRules`, `suppressNotifications`, JSON Patch | ✅ | https://learn.microsoft.com/en-us/rest/api/azure/devops/wit/work-items/create?view=azure-devops-rest-7.1 |
| C2 | ADO auth: Entra ID preferred; service principals for non-Azure workloads; PATs "sparingly" | ✅ | …/authentication-guidance |
| C3 | ADO rate limits: 200 TSTU / 5 min sliding; `Retry-After`, `X-RateLimit-*` | ✅ | …/concepts/rate-limits |
| C4 | Remote ADO MCP server (preview) `https://mcp.dev.azure.com/{organization}`; non-interactive auth | 🟡 / ❓ | https://github.com/microsoft/azure-devops-mcp |
| C5 | PagerDuty local MCP server deprecated → hosted remote MCP (OAuth) | ✅ / ❓ endpoint | https://github.com/PagerDuty/pagerduty-mcp-server |
| C6 | ServiceNow Table/KM APIs, PagerDuty REST v2, Graph mail/drive/subscriptions/RBAC-for-Applications | ❓ (well-known APIs; not re-fetched) | canonical URLs in [09](09-connectors-and-integrations.md) | M1 connector spike |
| C7 | SLB Knowledge Base API | ❓ no public API | — | Adapter; Q-01 |

### 2.9 Methodology

| # | Finding | Status |
|---|---|---|
| B1 | A public, official Google document defining an "AI Black Belt methodology" with named phases and gates was **not found** in any fetched official source | ❓ — operationalized explicitly in [00](00-methodology-and-phase-gates.md); please supply the internal reference if one exists |

---

## 3. Conflicts with workspace rules / reference guides

| Workspace statement | Verified reality | Resolution |
|---|---|---|
| `GEMINI.md`: `GOOGLE_GENAI_USE_VERTEXAI=true` | Current name `GOOGLE_GENAI_USE_ENTERPRISE`; ADK says equivalent | Set **both**; code passes `enterprise=True` explicitly (rule honoured) |
| `GEMINI.md`: `genai.Client(enterprise=True, project="chayan-genai-bb", location="us-central1")` | Valid, verified | Used as-is in `dev` |
| Guides: "Agent Engine" | Renamed **Agent Runtime** | Docs use new name; CLI still `agent_engine` |
| Guides: `onUiComponentDelivery`, `Table`, `ConditionList` | Not A2UI / not basic catalog | Official messages + SLB custom catalog |
| Guides: `a2a.types` monkeypatch | Not documented | Pin compatible versions instead |
| Gemini skill: models `gemini-3.8-flash`, `gemini-3.5-flash-lite`, `gemini-3.1-pro-preview` | Present on Agent Platform (3.1 Pro = Preview) | GA models in prod paths only (ADR-008) |

---

## 4. Re-verification 2026-11-24 (after G2 approval)

> **Method.** Packages installed into `.venv` at the pins in `pyproject.toml` and **introspected directly** (`inspect.signature`, Pydantic `model_fields`), plus official sources re-downloaded (A2UI repo README + `specification/v1_0/`, ADK `CHANGELOG.md`). This is stronger evidence than documentation because it tests the code we ship against.

### 4.1 What changed since 2026-10-12

| # | Change | Evidence | Spec impact |
|---|---|---|---|
| R1 | **A2UI v1.0 is current** (spec header `Status: Current`; README links v1.0 spec; Google blog GA 2026-11-11) | `specification/v1_0/docs/a2ui_protocol.md`, README | **ADR-007 switched to v1.0**; doc 11 rewritten |
| R2 | v1.0 wire format: `{"version": "1.0", "createSurface": {surfaceId, catalogId, sendDataModel, components, dataModel}}`; bindings `{"@path": …}`, calls `{"@call": …}`, envelope keys `@catalogId`, `@accessibility`, `@metadata`, `@checks`; component ids follow UAX #31 (`user_card` ✅, `user-card` ❌); new RPC messages `callRendererFunction` / `callAgentFunction` | `json/agent_to_renderer.json`, `renderer_to_agent.json`, `common_types.json` | Surface builders emit v1.0 |
| R3 | A2A extension URI `https://a2ui.org/a2a-extension/a2ui/v1.0`; MIME `application/a2ui+json`; renderer capabilities in `message.metadata["a2uiRendererCapabilities"]` (version-keyed); **agent MUST NOT send A2UI on a turn without capabilities** | `extensions/a2a/docs/a2ui_extension_specification.md`; `a2ui.a2a.A2UI_EXTENSION_BASE_URI`, `A2UI_MIME_TYPE` | BFF sends capabilities every turn |
| R4 | A2UI Python SDK split: `a2ui-agent-sdk` (core/agent), `a2ui-adk` (`SendA2uiToClientToolset`, `A2uiInstruction`, processor stores), `a2ui-a2a` (`create_a2ui_part(s)`, `get_a2ui_agent_extension`, `renderer_capabilities_from_message`); builder `a2ui.builder.v1_0`; `BasicCatalog("1.0").catalog_id == "https://a2ui.org/specification/v1_0/catalogs/basic/catalog.json"` | installed-package introspection | Pins updated (doc 14 §3) |
| R5 | `google-adk` 2.15.0: Python ≥ 3.11; `google-genai >=2.34,<3`; new extras `agent-identity` (Agent Identity credential providers), `otel-gcp`; Workflow nodes support `require_confirmation` (2.12); A2A integration **stable** + `adk generate_agent_card` (2.14); breaking changes in 2.14/2.15 (enum tool args as members; Visual Agent Builder removed; `McpInstructionProvider` explicit prompt args) do **not** affect this design | PyPI metadata; `CHANGELOG.md` | Pin 2.15.0 |
| R6 | `mcp` 2.x renamed `FastMCP` → `MCPServer` (`from mcp.server.mcpserver import MCPServer`); `ToolAnnotations(read_only_hint, destructive_hint, idempotent_hint, open_world_hint)` | introspection + SDK error message | MCP servers use `MCPServer` |

### 4.2 Installed-API confirmations (google-adk 2.15.0)

| API | Confirmed signature (abridged) |
|---|---|
| `google.adk.Agent` | fields incl. `name, mode, model, instruction, tools, input_schema, output_schema, output_key, include_contents, generate_content_config, sub_agents, rerun_on_resume, retry_config, timeout` + all callbacks |
| `google.adk.Workflow` | `name, edges, max_concurrency, rerun_on_resume (default True), input_schema, output_schema, state_schema` |
| `google.adk.workflow.node` | `node(node_like=None, *, name, rerun_on_resume, retry_config, timeout, parallel_worker, max_parallel_workers, auth_config, require_confirmation, parameter_binding='state'\|'node_input')` |
| `google.adk.workflow.FunctionNode` | `FunctionNode(*, func, name, rerun_on_resume, retry_config, timeout, auth_config, require_confirmation, parameter_binding, state_schema)` — **import path now confirmed** (was ❓) |
| `google.adk.Context.run_node` | `run_node(node, node_input=None, *, use_as_output, run_id, use_sub_branch, …)` |
| `google.adk.events.RequestInput` | `RequestInput(interrupt_id, payload, message, response_schema)` |
| `google.adk.apps.App` | `App(name, root_agent, plugins, events_compaction_config, context_cache_config, resumability_config)` |
| `BasePlugin` hooks | keyword-only: `before_tool_callback(*, tool, tool_args, tool_context)`, `after_tool_callback(*, tool, tool_args, tool_context, result)`, `before/after_model_callback(*, callback_context, llm_request/llm_response)`, + `on_user_message`, `before/after_run`, `on_event`, error hooks |
| `McpToolset` | `McpToolset(*, connection_params, tool_filter, tool_name_prefix, auth_scheme, auth_credential, require_confirmation, header_provider, …)`; `StreamableHTTPConnectionParams(url, headers, timeout, sse_read_timeout, …)` |
| `RemoteA2aAgent` / `to_a2a` | `RemoteA2aAgent(name, agent_card, *, description, timeout, …)`; `to_a2a(agent \| Workflow, *, host, port, agent_card, runner, security_schemes, …)` |
| `ModelArmorPlugin` | `ModelArmorPlugin(*, config: ModelArmorConfig, name, client, credentials)` |
| `VertexAiSessionService` | `(project, location, agent_engine_id, *, express_mode_api_key)` |

### 4.3 Items resolved

| Was | Resolution |
|---|---|
| **A8 / SP-4** exact resume payload for `RequestInput` | **RESOLVED by executed probe** (zero-LLM workflow, ADK 2.15.0): the pause is an event whose `long_running_tool_ids` holds the interrupt id and whose part is `function_call(name="adk_request_input", id=<interruptId>, args={interruptId, payload, message, response_schema})`. Resume = `runner.run_async(..., invocation_id=<same>, new_message=Content(role="user", parts=[Part(function_response=FunctionResponse(id=<interruptId>, name="adk_request_input", response={...}))]))`. The response dict becomes the paused node's output and flows to its successor; completed nodes are replayed from checkpoint, not re-executed. Encoded in `pm_agent.hitl.resume` + test. |
| `FunctionNode` import path | `from google.adk.workflow import FunctionNode` |
| A2UI v1.0 renderer support | React, Angular, Lit packages published (`@a2ui/react`, `@a2ui/angular`, `@a2ui/lit`) |

### 4.4 Still unconfirmed (unchanged; placeholders in code)

`adk eval` with a `Workflow` root (SP-7) · Cloud Run/GKE as Agent Gateway **sources** · Agent Identity Auth Manager specifics for third-party OAuth (SP-9; ADK now ships an `agent-identity` extra to evaluate) · per-model `us-central1` availability (SP-2) · SLB Knowledge Base API (Q-01) · "Envoy-based" gateway.

## 5. Implementation-time verification (2026-11-24, reference implementation)

Everything below was confirmed by executing code against the installed packages (offline probes and the test suite), not by reading docs.

| Area | Confirmed behaviour | Evidence |
|---|---|---|
| ADK `Agent` as a node | `ctx.run_node(Agent(mode="single_turn", output_schema=M), dict)` returns the parsed **dict**; the dict reaches the model as JSON text even with `include_contents="none"`; a custom `BaseLlm` subclass can stand in for Gemini | probe + [`agents/offline.py`](../src/pm_agent/agents/offline.py) |
| Dynamic orchestration + HITL | A `@node(rerun_on_resume=True)` node can loop and pause repeatedly on `RequestInput` children; completed children replay from checkpoint on every resume | probe + [`test_define_requirement.py`](../tests/component/test_define_requirement.py) |
| Fan-out / join | `("START", a, (b, c), JoinNode(name=…), d)`; the join emits `{predecessor_name: output}` | probe + [`ingestion_app/agent.py`](../src/pm_agent/ingestion_app/agent.py) |
| Node naming | `Context.run_node` has `run_id=` (no `name=`); closures are named with `FunctionNode(func=…, name=…)`; function parameters other than `node_input`/`ctx` bind from session state by default (`parameter_binding="state"`) — node functions therefore take only `node_input` | installed signatures |
| Plugins | `BasePlugin` hooks are async and keyword-only (`before_tool_callback(*, tool, tool_args, tool_context)`); `ToolContext.agent_name` identifies the calling agent | [`grounding/plugin.py`](../src/pm_agent/grounding/plugin.py) |
| google-genai embeddings | `client.models.embed_content(model=…, contents=[…], config=EmbedContentConfig(task_type="SEMANTIC_SIMILARITY", output_dimensionality=768))` → `embeddings[i].values` | installed signatures, [`correlation/scoring.py`](../src/pm_agent/correlation/scoring.py) |
| A2UI v1.0 SDK | Surfaces validate against the vendored v1.0 JSON schemas **and** the SDK's strict `MessageProcessor`; a `createSurface` for a live id is an integrity error; the SLB catalog loads via `importlib.resources` and is accepted by `Catalog.from_json(…, "1.0")` | [`tests/contract/test_a2ui.py`](../tests/contract/test_a2ui.py) |
| MCP 2.3 | `FastMCP` renamed `MCPServer` (`mcp.server.mcpserver`); `ToolAnnotations` carry the read-only/destructive/idempotent hints used by Gateway CEL | MCP contract tests |
| JWS / ES256 | Approval tokens and verifier reports sign/verify with P-256 raw `r‖s` signatures; `alg` is pinned (no `none`/HMAC confusion) | [`test_hitl_ledger_tokens.py`](../tests/unit/test_hitl_ledger_tokens.py) |

**Still UNCONFIRMED after implementation** (tracked as placeholders or doc notes): A2A `DataPart` shape for A2UI (array vs one message per part — we follow `a2ui-a2a`); `@a2ui/angular` handling of custom SLB components; IAP JWT group claims under Workforce Identity Federation (Q-08 — `PlaceholderGroupResolver`); Agent Gateway UAP V2 vs legacy IAP V1 policies (Terraform supports both, [08 §3.2](08-security-identity-and-governance.md)); Figma private-plugin distribution and auth (Q-02); live Gemini structured-output acceptance of the LLM schemas in [`agents/llm_schemas.py`](../src/pm_agent/agents/llm_schemas.py) (first dev-project run, Q-10).
