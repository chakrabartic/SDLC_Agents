# 14 — Implementation Roadmap (post-G2)

> Nothing in this document starts until gate **G2** is approved. Work proceeds in small, test-first increments; every milestone ends with a demo, green gates, and a **prompt to the user to confirm a Git commit/push to GitHub** (workspace rule — no automatic pushes).

---

## 1. Planned repository layout

```text
SLB/
├── README.md                      # entry point (spec map, status)
├── docs/                          # this specification (source of truth)
├── pyproject.toml                 # pinned deps (see §3); tool config: pylint, mypy --strict, pytest
├── src/pm_agent/
│   ├── config/                    # pydantic-settings; model ids; config.* loaders (versioned)
│   ├── schemas/                   # Pydantic v2 models from docs/10 (Signal, Claim, Citation, drafts, HITL)
│   ├── grounding/                 # GroundingGuardPlugin, atom extractors, citation checks (shared with verifier)
│   ├── hitl/                      # approval-token verification lib, ledger client
│   ├── a2ui/                      # SLB catalog schema, surface builders, payload validation, part conversion
│   ├── core_app/                  # ADK App `pm_agent_core` (agent.py exposes `app`)
│   │   ├── orchestrator.py
│   │   ├── workflows/             # problem_builder, define_requirement, prototype, investigation, knowledge, trace, trends
│   │   └── llm_nodes/             # one module per single-turn LLM node: instruction + output_schema
│   ├── ingestion_app/             # ADK App `signal_ingestion`
│   └── executor_app/              # ADK App `commit_executor` (function nodes only)
├── services/                      # Cloud Run (MCP servers + services)
│   ├── mcp_ado/  mcp_ado_write/  mcp_servicenow/  mcp_pagerduty/  mcp_msgraph/  mcp_msgraph_write/
│   ├── mcp_kb/  mcp_kb_write/  mcp_figma/  mcp_figma_write/
│   ├── mcp_store/  mcp_verifier/  mcp_ledger/  svc_prototype_renderer/
│   └── pm_workbench/              # bff/ (Python) + web/ (Angular A2UI renderer + SLB catalog components)
├── gke/                           # signal_event_gateway/, correlation_engine/, dls_catalog_sync/ (+ manifests)
├── figma_plugin/                  # private SLB org plugin (TypeScript) — renderer path A
├── infra/terraform/               # modules + envs (docs/12 §3)
├── evals/                         # fixtures (sanitized), goldens S1–S7, suites E1–E8, ADK evalsets
├── tests/                         # unit/ contract/ component/ integration/
└── .venv/                         # local virtualenv (Python 3.13; already created)
```

---

## 2. Milestones

| M | Name | Scope (bite-sized, test-first) | Exit criteria | Covers |
|---|---|---|---|---|
| **M0** | Foundations | Repo scaffold + pins; CI (lint, types, unit, policy lint); `schemas/` with property tests; Terraform: network, perimeter, KMS, AlloyDB, Pub/Sub, identity pools; Agent Registry + Agent Gateway (**dry-run**); hello-world ADK app on Agent Runtime with `AGENT_IDENTITY` + `agent_gateway_config`, calling one Cloud Run MCP echo server through egress | CI green; echo tool call visible in Gateway telemetry with SPIFFE principal; spikes SP-1…SP-4 closed | DONE-8 (skeleton), DONE-9 (skeleton) |
| **M1** | Read connectors & ingestion | `mcp-ado`, `mcp-servicenow`, `mcp-pagerduty`, `mcp-msgraph` (read) with recorded fixtures; `mcp-verifier.redact` (SDP + custom infoTypes); `mcp-store.put_signals` (chunk, embed, index); `signal_ingestion` W1; `signal-event-gateway` (GKE) + Pub/Sub hints; cursors | Contract tests per connector; E7 ≥ thresholds; ingestion of S1–S5 fixtures end-to-end | DONE-1 (ingest/normalize/dedup), IR1–IR4 |
| **M2** | Correlation & problem views | `correlation-engine` (GKE) + `mcp-correlation`; W2; `GroundingGuardPlugin`; verifier V1–V5 (`check_claims`); Problem Inbox/Detail A2UI surfaces; Workbench BFF (IAP, A2A streaming) | E1 thresholds; AC1 (one problem per seeded scenario); claims rendered with chips | DONE-1, FR-A2/A3, FR-C1 |
| **M3** | Readiness & requirement drafting | W3 to the proposal step; SLB templates in `config.*`; V0–V10 incl. near-duplicate gate; Readiness & Review surfaces; `pm_input` evidence | E2, E3 (0 uncited), E4 baseline; AC2, AC8 | DONE-2, DONE-3, DONE-7 |
| **M4** | HITL ledger & commit | `mcp-ledger` (KMS tokens, hash chain, anchors, queries); BFF `decide` path; `commit_executor` app; `mcp-ado-write` (`validateOnly` pre-flight); Gateway CEL policies **enforced** in dev/stage; E6 safety suite | AC3, AC9, AC10; E6 = 0 violations | DONE-5, DONE-6, DONE-8, R2 |
| **M5** | Operations & knowledge | W5 investigation; sub-workflows K/T/R; `mcp-kb` + `mcp-kb-write` for the confirmed backend (Q-01); traceability explorer | AC5, AC6, AC7 | R5, FR-C1–C8 |
| **M6** | Prototype validation | `dls-catalog-sync`; `mcp-figma` (read) / `mcp-figma-write` (comments); W4; `svc-prototype-renderer` + private Figma plugin (path A); `PrototypePreview`; feedback webhooks | E5; AC4 | DONE-4, R4, FR-B1–B6 |
| **M7** | Hardening & production | Stage UAT with pilot PMs; live E1–E8; pen-test; load & chaos; runbooks; DR drill; Gateway **enforce** in prod; progressive rollout | G4 then G5 checklists signed | R1, NFR-6–10 |

Indicative duration: M0–M4 ≈ 10–12 weeks to a governed R2 pilot; M5–M7 ≈ 8–10 weeks (to baseline with SLB capacity).

---

## 3. Dependency pins (re-verified 2026-11-24; source of truth = `pyproject.toml`)

| Package | Pin | Reason |
|---|---|---|
| `google-adk[a2a,mcp,gcp,agent-identity,otel-gcp]` | `==2.15.0` | Current 2.x GA line; `gcp` → `google-cloud-agentplatform[agent-engines]`; `agent-identity` → Agent Identity credential providers; requires Python ≥ 3.11 |
| `google-genai` | `==2.34.0` | `enterprise=True`; ADK 2.15 requires `>=2.34,<3` — **google-genai 3.x exists but is not yet compatible with ADK** |
| `a2ui-agent-sdk` | `==0.12.1` | A2UI v1.0 agent SDK (pre-1.0 package; exact pin) — pulls `a2ui-core 0.7.x` |
| `a2ui-adk[a2a]` | `==0.3.0` | Official A2UI ↔ ADK integration (split out of the agent SDK in Oct 2026) |
| `a2ui-a2a` | `==0.4.0` | Official A2UI ↔ A2A integration |
| `a2a-sdk` | `==0.3.26` | `a2ui-a2a` requires `>=0.3.0,<0.4.0` (A2A 1.x SDK not yet supported by the A2UI integration) |
| `mcp` | `>=2.3,<3` | MCP servers (FastMCP, streamable HTTP); ADK `mcp` extra allows `<3` |
| `rfc8785`, `cryptography` | `==0.1.4`, `>=46` | Canonical JSON (JCS) hashing; ES256 approval tokens |
| Platform clients (`google-cloud-dlp`, `-kms`, `-pubsub`, `-bigquery`, AlloyDB connector) | added when the adapter is implemented | Real adapters replace local fakes |
| Test/lint | `pytest`, `pytest-asyncio`, `pytest-mock`, `pylint`, `mypy` | Workspace standard |

Runtime: Python **3.13** in containers and `.venv` (local default `python3` is 3.14; avoided until all dependencies publish wheels).

---

## 4. Spikes for unconfirmed items

| Spike | Question | Milestone | Fallback if negative |
|---|---|---|---|
| SP-1 | Client construction for `runtimes.create` in the split `google-cloud-agentplatform` 2.x SDK | M0 | `adk deploy agent_engine` CLI in CI |
| SP-2 | `gemini-3.8-flash`, `gemini-3.5-flash-lite`, `gemini-embedding-2` availability on `us-central1` | M0 | `us` multi-region / `global` with SLB residency sign-off; `gemini-embedding-001` |
| SP-3 | Cloud Run IAP-protected MCP server reachable via Agent Gateway egress with tool-level CEL | M0 | GKE internal service registered as endpoint |
| SP-4 | Exact client FunctionResponse for `adk_request_input` resume on Agent Runtime | M0 | Ledger-driven resume with a fresh invocation carrying `action_id` |
| SP-5 | Graph Excel workbook API with application permissions | M1 | Download + parse `.xlsx` |
| SP-6 | Remote ADO MCP with service-principal auth | M1 | Keep own `mcp-ado` |
| SP-7 | `adk eval` with a `Workflow` root | M2 | Custom Runner-based harness |
| SP-8 | Figma Plugin API rendering of SLB DLS (fonts, image components) + Figma MCP Catalog admission | M6 (apply in M0) | Plugin path only |
| SP-9 | Agent Identity Auth Manager for third-party OAuth (Entra, Figma) | M1 | Secret Manager + per-server credentials |

---

## 5. Definition of Done (every task)

1. Failing test written first, now green; no skipped tests.
2. `pylint` ≥ 9.5, `mypy --strict` clean, policy lint clean.
3. Relevant eval suite re-run in replay mode; thresholds met.
4. Spec/ADR updated in the same PR if behaviour or interfaces changed.
5. Ledger entries and traces emitted for any new action path.
6. Reviewed by one engineer; security review for anything touching identity, tokens, write paths or redaction.
