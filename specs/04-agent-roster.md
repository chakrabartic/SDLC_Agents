# 04 — Agent Roster & Justification

> Derived from the requirements' logical composition (§7) — **PM Orchestrator, Signal Ingestion & Normalization, Correlation / Problem Builder, Requirement Drafting & Feasibility, Prototype (UX), Operations, Knowledge & Roadmap, Grounding/Verifier Layer, HITL Approval & Audit Ledger** — and mapped onto verified ADK 2.x constructs (`Workflow` graphs, `@node` dynamic workflows, `Agent(mode="single_turn")`, `RequestInput`). See [02](02-platform-verification-log.md) for API verification.

---

## 1. Design principles for the roster

1. **Code orchestrates, models judge.** Sequencing, gating, retries, state transitions and every number are deterministic code (ADK function nodes). LLM nodes are narrow, single-turn, schema-bound judgment steps (`output_schema` = claim-structured Pydantic models).
2. **One responsibility, one identity boundary.** Components that can *change external systems* never share a process or identity with components that *read untrusted content and reason over it* (assess-before-mutation, ADR-003).
3. **Connectors are tools, not agents.** Fetching from ADO/ServiceNow/PagerDuty/Graph needs no judgment, so connectors are MCP servers, not LLM agents.
4. **The gatekeepers are not agents.** The Verifier and the Ledger are deterministic services the agents must pass through; an LLM cannot talk its way past them.

---

## 2. Roster at a glance

| # | Component (ADK name) | Deployment · identity | ADK 2.x construct | LLM nodes (model) | Mutation class |
|---|---|---|---|---|---|
| 1 | **PM Orchestrator** (`pm_orchestrator`) | `pm-agent-core` · `AID-CORE` | Dynamic workflow `@node(rerun_on_resume=True)` + intent router | `intent_router` (3.5 Flash-Lite), `pm_assistant` (3.8 Flash) | None |
| 2 | **Signal Ingestion & Normalization** (`signal_ingestor`) | `signal-ingestion` · `AID-INGEST` | Graph `Workflow` of function nodes, fan-out/`JoinNode` per source | `type_hint_classifier` (3.5 Flash-Lite, optional) | Internal store writes only |
| 3 | **Correlation / Problem Builder** (`problem_builder`) | `pm-agent-core` · `AID-CORE` (+ background mode) | Graph `Workflow` with score-based routes | `link_adjudicator`, `problem_characterizer` (3.8 Flash) | Internal store writes only |
| 4 | **Requirement Drafting & Feasibility** (`requirement_drafter`) | `pm-agent-core` · `AID-CORE` | Dynamic workflow with `RequestInput` pauses | `readiness_assessor`, `question_generator`, `spec_writer`, `feasibility_assessor`, `impl_planner` (3.8 Flash) | Draft only |
| 5 | **Prototype (UX)** (`prototype_planner`) | `pm-agent-core` · `AID-CORE` | Dynamic workflow with `RequestInput` pauses | `experience_translator`, `spec_composer` (3.8 Flash) | Draft only (render via executor) |
| 6 | **Operations** (`operations_analyst`) | `pm-agent-core` · `AID-CORE` | Graph `Workflow` | `engagement_advisor`, `outcome_classifier` (3.8 Flash) | Draft only |
| 7 | **Knowledge & Roadmap** (`knowledge_roadmap_curator`) | `pm-agent-core` · `AID-CORE` | Three sub-workflows (knowledge, trace, trends) | `kb_writer`, `trace_linker`, `trend_narrator` (3.8 Flash) | Draft only |
| 8 | **Grounding / Verifier Layer** | `mcp-verifier` (Cloud Run) · `SA-VERIFIER` + `GroundingGuard` ADK plugin | Deterministic service + global plugin | bounded entailment judge only (3.8 Flash, pinned) | Gate (writes only its own reports) |
| 9 | **HITL Approval & Audit Ledger** | `mcp-ledger` (Cloud Run) · `SA-LEDGER` + `commit-executor` (Agent Runtime) · `AID-COMMIT` | Service + **zero-LLM** graph `Workflow` | **none** | **Commit on approval (sole writer)** |

`AID-*` = Agent Identity (SPIFFE) of an Agent Runtime deployment; `SA-*` = Cloud Run service identity. Permissions per identity: [08 §3](08-security-identity-and-governance.md#3-identity-model).

---

## 3. Component contracts

### 3.1 PM Orchestrator — `pm_orchestrator`

| | |
|---|---|
| **Requirements** | §7 supervisor; FR-A10 canonical flow; R2 gating; NFR-6 latency |
| **Why separate** | Single owner of session/problem state and of *which* capability runs; prevents LLM-to-LLM transfer loops (deterministic dispatch instead of free `transfer_to_agent`). |
| **Construct** | `@node(rerun_on_resume=True) async def pm_orchestrator(ctx, node_input)` — calls `ctx.run_node(intent_router, msg)` (single-turn, `output_schema=Intent`) then dispatches deterministically to capability workflows; checkpointing skips completed children on resume after HITL pauses. |
| **Tools** | `store.get_problem`, `store.list_problems`, `evidence.search` (via `pm_assistant`), `ledger.query` |
| **Outputs** | A2UI surfaces (problem inbox, problem detail, progress), claim-structured answers |
| **HITL** | Hosts the human-input interrupts raised by capability workflows; never decides them |
| **Failure** | Unknown intent ⇒ clarify with PM; capability failure ⇒ structured error card, no partial output (NFR-8) |
| **Evals** | E8 intent routing accuracy ≥ 0.97; trajectory match on golden scenarios |

### 3.2 Signal Ingestion & Normalization — `signal_ingestor`

| | |
|---|---|
| **Requirements** | FR-A1, IR1–IR4, GOV4, GOV7, DONE-1 |
| **Why separate (own deployment)** | Runs continuously on schedule/events, holds the broadest *read* scopes, and must stay zero/low-LLM for throughput and injection-resistance; isolating it keeps raw-source access away from conversational agents. |
| **Construct** | Graph `Workflow`: `START → plan_window → (poll_ado ‖ poll_servicenow ‖ poll_pagerduty ‖ poll_mail ‖ poll_onedrive) → JoinNode → dedup → persist → assign_candidates → emit_events`. Triggered by Cloud Scheduler (delta windows) and by hints from `signal-event-gateway` (webhooks) via Pub/Sub. |
| **Tools** (read-only) | `ado.list_changed_work_items`, `ado.get_work_items`, `ado.list_comments`; `servicenow.list_records`, `servicenow.list_journal`; `pagerduty.list_incidents`, `pagerduty.list_notes`, `pagerduty.list_log_entries`; `msgraph.mail_delta`, `msgraph.get_message`, `msgraph.drive_delta`, `msgraph.get_item_text`, `msgraph.read_workbook`; `store.put_signals`, `correlation.assign_candidates` |
| **Outputs** | Versioned, redacted `Signal`s + `EvidenceChunk`s; `signal-ingested` events; per-source health (`sources_unavailable`) |
| **LLM use** | Optional `type_hint_classifier` (Flash-Lite) producing `signal_type_hint` with a `basis_span` into the signal itself |
| **Failure** | Per-source isolation: a failing connector marks the source unavailable and never blocks others (IR3); 429 ⇒ backoff with `Retry-After` |
| **Evals** | E7 redaction recall; normalization contract tests per connector; dedup precision (E1) |

### 3.3 Correlation / Problem Builder — `problem_builder`

| | |
|---|---|
| **Requirements** | FR-A2, FR-A3, FR-A4, FR-C1, FR-C8, G3, DONE-1 |
| **Why separate** | Correlation quality is the foundation of every downstream draft; it combines deterministic scoring with a narrow adjudication model and must be evaluable on its own (E1). |
| **Construct** | Graph `Workflow`: `get_candidates (correlation.find_candidates) → score → route {AUTO_LINK, ADJUDICATE, NEW}` → `link_adjudicator` (grey zone only) → `aggregate` (code: customers, regions, frequency) → `problem_characterizer` (title, classification, impact claims) → `verify (verifier.check_claims)` → `upsert (store.upsert_problem)`. Runs **interactively** and in **background** (scheduled drain of the adjudication queue). |
| **Tools** | `correlation.find_candidates`, `correlation.related`, `store.upsert_problem`, `store.link_signals`, `evidence.search`, `verifier.check_claims`, `verifier.near_duplicates` |
| **Outputs** | `ProductProblem` (versioned), `DuplicateCandidate`s, low-confidence items queued for PM confirmation |
| **HITL** | PM confirms merges below threshold and low-confidence classifications (requirements §13) |
| **Evals** | E1 pairwise precision ≥ 0.95 / recall ≥ 0.85; seeded multi-source scenario ⇒ exactly one problem (AC1) |

### 3.4 Requirement Drafting & Feasibility — `requirement_drafter`

| | |
|---|---|
| **Requirements** | FR-A4–A10, GOV2, GOV3, AC2, AC3, DONE-2, DONE-3 |
| **Why separate** | Owns the canonical flow *Problem → gaps → PM judgment → spec → approval → ADO* and the SLB templates; feasibility stays here because it reasons over the same evidence pack. |
| **Construct** | Dynamic workflow: `prior_art` → `readiness_assessor` → `verify` → *(gaps)* `question_generator` → **`RequestInput` (PM answers)** → `spec_writer` (template-bound output) → `feasibility_assessor` → `verifier.verify_subject` (V0–V10; ≤ 2 bounded repair loops, then fields marked `missing`) → `ledger.propose(ADO_CREATE_WORK_ITEM)` → **`RequestInput` (approval outcome)** → A2A `commit-executor.execute` → `impl_planner` (FR-A9, advisory, after approval). |
| **Tools** | `evidence.search`, `ado.search_work_items` (WIQL), `kb.search_articles`, `msgraph.search_documents` (architecture docs), `store.save_draft`, `verifier.*`, `ledger.propose`, `ledger.query` |
| **Outputs** | `ReadinessAssessment`, `ClarifyingQuestion`s, `RequirementDraft` (SLB template), `FeasibilityAssessment`, `ImplementationPlanDraft` |
| **HITL** | (1) PM answers/judgment; (2) approve/edit/reject the draft; (3) outreach drafts to SMEs are separate `OUTREACH_SEND_INTERNAL` actions |
| **Evals** | E2 readiness per-item F1 ≥ 0.90; E3 groundedness (0 uncited); E4 rubric (Engineering-style review) |

### 3.5 Prototype (UX) — `prototype_planner`

| | |
|---|---|
| **Requirements** | FR-B1–B6, AC4, DONE-4 |
| **Why separate** | Different evidence (approved requirement + SLB DLS catalog), different reviewers (UX), and a different write target (Figma) with its own constraints (ADR-006). |
| **Construct** | Dynamic workflow: guard (requirement `committed`/approved) → `experience_translator` (`PrototypeBrief`) → `figma.search_dls` (indexed SLB DLS library) → `spec_composer` (`PrototypeSpec`: component keys, variants, cited copy, new-pattern justifications) → `verifier.verify_subject` (V9: keys exist, reuse ratio, justifications) → `ledger.propose(FIGMA_RENDER_PROTOTYPE)` → **`RequestInput`** → render (executor: compiled Plugin-API program; see ADR-006) → `figma.render_images` → `ValidationPackage` → `ledger.propose(PROTOTYPE_RELEASE_INTERNAL, FIGMA_POST_COMMENT)` → feedback loop from Figma `FILE_COMMENT` webhooks (FR-B5). |
| **Tools** | `figma.search_dls`, `figma.get_component`, `figma.get_file_nodes`, `figma.render_images` (REST, read), `store.save_draft`, `verifier.*`, `ledger.propose` |
| **Outputs** | `PrototypeBrief`, `PrototypeSpec`, `ValidationPackage` |
| **HITL** | Approve render (PM + UX, configurable two-person rule); approve internal release. **External sharing is always done by humans** (FR-B6). |
| **Evals** | E5 DLS reuse ratio ≥ 0.90, 100 % justification coverage for new patterns, 0 invalid component keys |

### 3.6 Operations — `operations_analyst`

| | |
|---|---|
| **Requirements** | FR-C1, FR-C2, FR-C3, FR-C5, AC5 |
| **Why separate** | Investigation is triggered by individual issues (not problems) under time pressure; it is KB-first and ends in an *outcome classification* that routes to bug, requirement or knowledge flows. |
| **Construct** | Graph `Workflow`: `kb_first (kb.search_articles + store prior resolutions)` → `correlate (correlation.related)` → `assemble_pack` (code: related tickets, comments, emails, timeline) → `engagement_advisor` (who to engage — from authorship/resolution evidence, pseudonymized) → `outcome_classifier` → **`RequestInput` (PM confirms outcome)** → route `{defect → requirement_drafter(bug template), non_defect → requirement_drafter, resolution → knowledge capture, workaround → knowledge capture}`. |
| **Tools** | `kb.search_articles`, `correlation.related`, `evidence.search`, `servicenow.list_journal`, `pagerduty.list_notes`, `ado.list_comments` (fresh reads), `store.save_draft` |
| **Outputs** | `InvestigationPack` |
| **Evals** | AC5: prior resolution surfaced before drafting on recurring-issue scenarios (S1, S3) |

### 3.7 Knowledge & Roadmap — `knowledge_roadmap_curator`

| | |
|---|---|
| **Requirements** | FR-C4, FR-C6, FR-C7, FR-C8, AC6, AC7 |
| **Why separate** | Converts *approved* resolutions into organisational knowledge and maintains the evidence chain to roadmap decisions — long-lived, cross-problem responsibilities distinct from per-issue investigation. |
| **Construct** | Sub-workflow **K** (knowledge): guard (approved resolution) → `kb_writer` → `verifier.near_duplicates (KB)` → `verify_subject` (P-04 audience policy) → `ledger.propose(KB_PUBLISH_ARTICLE)` → **`RequestInput`** → executor. Sub-workflow **T** (trace): deterministic link maintenance from store events + `trace_linker` for semantic links (status `proposed`) → PM confirmation. Sub-workflow **R** (trends): BigQuery aggregates (code) → `trend_narrator` (claims citing aggregates) — advisory only (GOV3). |
| **Tools** | `kb.search_articles`, `trace.query`, `trace.propose_links`, `analytics.run_named_query` (BigQuery, parameterized, allow-listed), `store.save_draft`, `verifier.*`, `ledger.propose` |
| **Outputs** | `KnowledgeArticleDraft`, `TraceLink`s, trend briefs |
| **Evals** | AC6/AC7 scenarios; trace completeness ≥ 0.90 on golden chains (KPI "Roadmap evidence completeness") |

### 3.8 Grounding / Verifier Layer — `mcp-verifier` + `GroundingGuard`

Specified in [06](06-grounding-verifier-layer.md). **Why not an agent:** a verifier that is itself an LLM agent grades its own kind and can be steered by the same injected content; a deterministic service with a bounded, tool-less judge cannot.

### 3.9 HITL Approval & Audit Ledger — `mcp-ledger` + `commit-executor`

Specified in [07](07-hitl-approval-and-audit-ledger.md). **Why the executor is a separate zero-LLM deployment:** it is the *only* identity allowed to call write tools; having no model means it has no prompt-injection surface, and its behaviour is fully testable as ordinary code.

---

## 4. Alternatives considered and rejected

| Alternative | Decision | Reason |
|---|---|---|
| One agent per source system (ADO agent, ServiceNow agent…) | Rejected | Fetching needs no judgment; MCP connectors are cheaper, testable and policy-addressable at tool level |
| LLM coordinator with free `transfer_to_agent` between all sub-agents | Rejected | Non-deterministic routing loops (warned in `orchestration.md`); weak auditability. Kept only the intent classification as an LLM step. |
| LLM-based "verifier agent" | Rejected | Self-grading, bypassable; replaced by deterministic V0–V10 + bounded judge |
| Separate *Clarification/Outreach* agent | Folded into Requirement Drafting | Questions are an output of gap analysis; sending is an executor action |
| Separate *Prototype Builder* LLM agent writing to Figma | Rejected | LLM-authored canvas code is unverifiable; rendering is a deterministic compile of an approved, verified `PrototypeSpec` (ADR-006) |
| Autonomous roadmap prioritization agent | Out of scope | GOV3 / §4.2: agent advises, PM decides |
| ADK tool confirmation (`require_confirmation`) as the HITL mechanism | Not used for commits | Verified limitation: not supported with `VertexAiSessionService`/`DatabaseSessionService`; graph `RequestInput` + ledger tokens give stronger, auditable binding |

---

## 5. Shared configuration (all agent apps)

| Setting | Value |
|---|---|
| ADK | `google-adk==2.15.*` (pinned; upgrade via ADR + full eval run — re-verified 2026-11-24) |
| App | `App(name=…, root_agent=…, plugins=[GroundingGuardPlugin(), ModelArmorPlugin(…), LoggingPlugin(), BigQueryAgentAnalyticsPlugin(…)], resumability_config=ResumabilityConfig(is_resumable=True), events_compaction_config=…, context_cache_config=…)` |
| Sessions / memory | `VertexAiSessionService` (us-central1); `VertexAiMemoryBankService` for PM preferences only (DM-6) |
| Model client | `google-genai` with `enterprise=True`, `project="chayan-genai-bb"` (per env), `location="us-central1"`; pinned model ids (no `*-latest` aliases on regional endpoints) |
| LLM node defaults | `mode="single_turn"`, `include_contents="none"` (inputs passed explicitly), `output_schema` = claim-structured model, `temperature ≤ 0.2`, `retry_options` on 429/5xx |
