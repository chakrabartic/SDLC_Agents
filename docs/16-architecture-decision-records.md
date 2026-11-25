# 16 — Architecture Decision Records

> Status of all ADRs: **Accepted** at gate G2 (2026-11-24). ADR-007 was revised before acceptance to target A2UI v1.0 (GA 2026-11-11). SLB-specific values that are still unknown are carried as **placeholders** (see `PLACEHOLDERS.md`), not as open decisions. Format: context → decision → alternatives → consequences.

---

### ADR-001 — ADK 2.x graph & dynamic workflows as the orchestration backbone

* **Context.** R2 needs structurally guaranteed gating; R6 needs deterministic numbers and auditable steps; HITL pauses may last days. ADK 2.0 (GA 2026-05-19) introduced a graph Workflow runtime with `RequestInput` pauses, routes, `JoinNode`, dynamic `@node` workflows and checkpointed resume.
* **Decision.** Supervisor = dynamic workflow (`@node(rerun_on_resume=True)` + `ctx.run_node`) with one single-turn intent classifier; capabilities = graph/dynamic workflows; LLM nodes are `mode="single_turn"` with `output_schema`.
* **Alternatives.** LLM coordinator with free `transfer_to_agent` (non-deterministic loops, weak audit); template agents (Sequential/Parallel/Loop — superseded in 2.0); custom `BaseAgent._run_async_impl` (bypassed in 2.0).
* **Consequences.** Deterministic, testable control flow; explicit loop bounds required; dependence on 2.x APIs that still evolve (pin minor, ADR for upgrades); `adk eval` with Workflow roots to be confirmed (SP-7).

### ADR-002 — Runtime placement: Agent Runtime for agents, Cloud Run for tool servers, GKE for data-plane containers

* **Context.** DONE-8 requires every tool call through Agent Gateway with a SPIFFE identity; DONE-9 requires Cloud Run + GKE. Verified: Gateway ingress supports Agent Runtime agents only; egress sources are Agent Runtime and Gemini Enterprise; Cloud Run can be an IAP-protected MCP destination (Preview).
* **Decision.** `pm-agent-core`, `signal-ingestion`, `commit-executor` on **Agent Runtime** (`identity_type=AGENT_IDENTITY`, `agent_gateway_config` at first deploy). All MCP tool servers, verifier, ledger, renderer and workbench on **Cloud Run**. Webhook fan-in, correlation engine and catalog sync on **GKE Autopilot**.
* **Alternatives.** ADK agents on Cloud Run/GKE (`adk deploy cloud_run/gke` exist) — rejected because Gateway governance of those sources is not documented; everything on GKE — loses managed Sessions/Memory Bank/identity.
* **Consequences.** Full Gateway governance today; three runtimes to operate (Terraform-managed); revisit if Gateway documents Cloud Run/GKE sources.

### ADR-003 — Identity split: read-only analysis vs. a single zero-LLM write executor

* **Context.** Ingested content is untrusted (GOV7); any agent that reads it can be injected.
* **Decision.** `AID-CORE` and `AID-INGEST` get read-only connector tools (CEL `mcp.tool.isReadOnly == true`); only `AID-COMMIT` may call write tools (CEL on exact `mcp.toolName`), and only with an approval token; `commit-executor` contains no model.
* **Alternatives.** Per-agent write tools gated by ADK tool confirmation (unsupported with `VertexAiSessionService`; same identity reads and writes); one executor per target system (more isolation, more ops — possible later).
* **Consequences.** Prompt injection cannot reach a write; executor is plain, fully testable code; executor concentrates write scopes (mitigated by tokens + CEL + per-server credentials).

### ADR-004 — Deterministic Verifier gate with a bounded entailment judge; fail-closed

* **Context.** R6 demands mechanical enforcement; mechanical checks alone can't prove semantic support.
* **Decision.** Claim/citation contract; V0–V4, V6–V10 deterministic; V5 = tool-less, quote-only judge (pinned Gemini 3.8 Flash; dual judge for high-risk targets) that can only remove claims; verifier is a separate service with its own identity; KMS-signed reports bound to content hashes; unavailable ⇒ block.
* **Alternatives.** LLM self-critique; LLM verifier agent; post-hoc warnings.
* **Consequences.** Zero-uncited guarantee is code-enforced; some content will be blocked and turned into questions (RK-07); judge calibration becomes an eval responsibility.

### ADR-005 — KMS-signed single-use approval tokens + hash-chained ledger in AlloyDB, anchored in locked storage

* **Context.** GOV1/GOV6/NFR-5 require approval binding and an immutable, queryable audit.
* **Decision.** Approvals only via IAP-authenticated BFF → `mcp-ledger.decide`; ES256 tokens via Cloud KMS binding `action_id`, payload hash, verifier report hash, tool scope, 15-min TTL, single use; ledger table insert-only with SHA-256 chain, anchors every 5 min to a locked Cloud Logging bucket and nightly signed exports to a Bucket-Locked GCS bucket; BigQuery for analytics.
* **Alternatives.** BigQuery-only ledger (no transactional single-use consumption); blockchain/ledger DB (operational overhead without added assurance here); Spanner (viable; AlloyDB chosen to share the evidence store engine).
* **Consequences.** Strong non-repudiation and replay protection; KMS and ledger availability become hard dependencies for writes (by design).

### ADR-006 — Figma rendering by deterministic compilation of a verified `PrototypeSpec`

* **Context.** Verified: Figma REST cannot create canvas content; remote MCP `use_figma` can, but only for clients in the Figma MCP Catalog, with per-user OAuth, a Full seat, and limits (20 kb/call, no images, no custom fonts). `use_figma` executes Plugin-API JavaScript.
* **Decision.** LLM produces only a `PrototypeSpec` (DLS component keys, variants, cited copy, justifications) → Verifier V9 → approval → `svc-prototype-renderer` compiles it into a deterministic Plugin-API program. **Path A (now):** private SLB Figma plugin fetches the token-gated program and renders it when the approving PM/UX runs it. **Path B (after admission):** `commit-executor` passes the same program to `use_figma`.
* **Alternatives.** LLM writes canvas JS directly (unverifiable); REST-only (impossible); manual design from a brief (fails DONE-4 intent).
* **Consequences.** DONE-4 met with official APIs today, with a human trigger that doubles as the approval; fully unattended generation awaits Figma admission (RK-01); DLS fonts/images favour path A.

### ADR-007 — A2UI v1.0 over A2A with deterministic decision surfaces (revised 2026-11-24)

* **Context.** On 2026-10-12 A2UI v0.9.1 was current and v1.0 a release candidate, so v0.9.1 was proposed. By 2026-11-24 **A2UI v1.0 is current** (GA 2026-11-11) with React/Angular/Lit renderers published, and the Python SDK is split into `a2ui-agent-sdk` / `a2ui-adk` / `a2ui-a2a` (the latter still pins `a2a-sdk` 0.3.x). No UI code had been written, so switching carries no migration cost. Approvals must show exactly what was verified.
* **Decision.** A2UI **v1.0** via the A2A extension (`https://a2ui.org/a2a-extension/a2ui/v1.0`, `application/a2ui+json`), renderer capabilities sent on every turn, Angular renderer, basic catalog + SLB custom catalog mixed per component via `@catalogId`; decision surfaces built by deterministic code; decision actions go straight to the ledger, never through an LLM.
* **Alternatives.** Stay on v0.9.1 (guaranteed migration later); custom JSON-RPC/SSE frames (`onUiComponentDelivery`, non-standard); LLM-generated approval UIs.
* **Consequences.** Standards-based UI on the current protocol with strong decision integrity; pre-1.0 SDK packages require exact pins and an adapter module; custom components still require renderer work.

### ADR-008 — Model selection & routing

* **Context.** Production requires GA models, pinned ids (aliases may fail on regional endpoints), cost control.
* **Decision.** `gemini-3.8-flash` (drafting, adjudication, judge), `gemini-3.5-flash-lite` (routing, type hints), `gemini-embedding-2` for embeddings (fallback `gemini-embedding-001` per SP-2); `gemini-3.1-pro-preview` only as an offline eval challenger (Preview — excluded from prod); all via `google-genai` `enterprise=True`; upgrades only through the eval gate.
* **Consequences.** Predictable quality/cost; embedding dimension fixed per index (re-embedding on change).

### ADR-009 — Evidence store on AlloyDB AI; Memory Bank is non-evidential

* **Context.** Citations need provenance, spans, versioning and hybrid retrieval; the reference guide's "shared epistemic memory" maps to Memory Bank, whose memories are model-generated.
* **Decision.** AlloyDB (pgvector + ScaNN + tsvector) for signals/chunks/problems/drafts/ledger; GCS for blobs by handle; BigQuery for analytics; Memory Bank only for PM preferences and never citable.
* **Alternatives.** Vector Search / RAG Engine (separate engine, weaker transactional coupling); BigQuery as primary store (no single-use transactions).
* **Consequences.** One transactional engine for evidence + HITL; Memory Bank use is bounded.

### ADR-010 — MCP as the uniform tool interface; own servers with read/write split

* **Context.** Agent Gateway parses MCP traffic for tool-level policy; vendors offer MCP servers with varying maturity (ADO remote preview; PagerDuty local deprecated; Figma client allow-list).
* **Decision.** Build thin, owned MCP servers per system, split into read and write servers with separately scoped credentials, server-side normalization/redaction/provenance, truthful annotations, token verification on writes. Vendor MCP servers may replace *read* servers after evaluation.
* **Consequences.** Consistent provenance and policy addressability; more services to maintain (template + shared library minimizes cost).

### ADR-011 — Ports & layering enforced by fitness tests (accepted 2026-11-24, code-health refactor)

* **Context.** A measured review found the read/write identity split of ADR-003 was conventional, not structural, in the reference implementation. Importing any LLM-side workflow loaded 9 write-side modules (approval-token minting, write guard, write servers, commit executor), because 11 modules imported the composition root `runtime.Services` and `pm_agent.hitl/__init__` eagerly re-exported everything. There were also two sub-package import cycles (`connectors ⇄ mcp_servers`, `renderer ⇄ runtime`). The verifier report travelled between workflow nodes in a hidden in-process dict (`services.reports`), which broke checkpointed resume after a restart. The policy lint's regexes (PL007–PL009) were the only guard.
* **Decision.**
  1. Agent-side code depends on **`pm_agent.ports`**: the `Services` bundle, typed by Protocols for every I/O adapter (`EvidenceRepository`, `ProblemStore`, `SubjectStore`, `IngestionState`, `IdentityVerifier`, `CommitClient`). The agent sees the approval service only through **`ApprovalsPort`** (propose + read). Deciding, minting and consuming tokens are reachable only from `runtime.LocalServices`, which the workbench BFF, the executor app, the renderer and tests hold.
  2. Only deployable entry points import the composition root. Packages the composition root depends on never import it: the renderer is composed by `runtime.renderer_app`.
  3. Package `__init__` modules on security boundaries don't eagerly re-export (`pm_agent.hitl`).
  4. Node outputs carry everything a later node needs. The signed verification report travels inside the checkpointed `verify` output, and is safe because it is self-authenticating: the ledger re-checks its ES256 signature, as `mcp-ledger.propose` does.
  5. Subject identity (`subject_digest`, `SUBJECT_REGISTRY`) lives in `schemas.subjects`, and report signing in `grounding.reports`, so write-side code can bind hashes without loading the verifier.
* **Enforcement.** `tests/architecture/test_layering.py` checks: no sub-package cycles; `schemas` is a leaf; only entry points import the composition root; and LLM-side packages never load a write-side module. The last is measured on the **real import closure** in a fresh interpreter, so transitive and `__init__` imports count. mypy strict makes `services.approvals.decide(...)` a type error on the agent side.
* **Alternatives.** Per-workflow dependency objects (more ceremony for one app with one set of adapters — rejected as over-engineering). import-linter (an extra dependency; it can't see the runtime closure). Keeping regex-only lint (bypassable by aliasing or line breaks).
* **Consequences.** Production adapters (AlloyDB stores, `mcp-ledger` client, A2A commit client) slot in behind existing Protocols without touching workflows. The trust boundary is checked on every CI run. Adding a port means updating `ports.py` and the local wiring in `runtime.py`.
