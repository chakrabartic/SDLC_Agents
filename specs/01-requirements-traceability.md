# 01 — Requirements Traceability Matrix

> Every requirement from the mission brief (R1–R6, DONE) and from `PM_Agent_Requirements.md` (FR, GOV, NFR, IR, AC) maps to a design element and a verification. Section links point into this spec.

---

## 1. Mission requirements (R1–R6)

| ID | Requirement | Design element | Verification |
|---|---|---|---|
| R1 | Production-ready, concept → production, phase gates | [00](00-methodology-and-phase-gates.md) gates G0–G6; [12](12-deployment-and-operations.md) Terraform, CI/CD, SLOs, runbooks, DR | Gate checklists; prod readiness review (G5) |
| R2 | Never autonomously create ADO items / publish knowledge / commit roadmap decisions; HITL draft-then-approve; assess-before-mutation | [07](07-hitl-approval-and-audit-ledger.md) H-1…H-7; [03 §4](03-system-architecture.md#4-assess-before-mutation-isolation-r2--four-independent-layers) four isolation layers; ADR-003 | E6 safety suite; ledger control query "COMMIT without TOKEN_CONSUMED" = 0 |
| R3 | Ingest ServiceNow/PagerDuty/ADO/Email/OneDrive; correlate → Product Problem; readiness checklist; questions; PRD/ADO drafts (SLB templates); feasibility | [04 §3.2–3.4](04-agent-roster.md#3-component-contracts); [05 W1–W3](05-orchestration-and-workflows.md#4-capability-workflows); [09](09-connectors-and-integrations.md); [10 §4.2](10-data-model-and-storage.md#42-slb-templates-derived-from-the-corpus--slb-to-confirm-canonical-versions-q-04) | E1, E2, E3, E4; S1–S5 |
| R4 | Figma prototypes aligned to SLB DLS; prefer reuse; justify new patterns; validation packages | [04 §3.5](04-agent-roster.md#35-prototype-ux--prototype_planner); [05 W4](05-orchestration-and-workflows.md#44-w4--prototype-validation-prototype_planner); ADR-006; policy P-07 | E5; S6 |
| R5 | Cross-system correlation, investigation assist, knowledge drafting, roadmap traceability | [04 §3.6–3.7](04-agent-roster.md#36-operations--operations_analyst); [05 W5–W7](05-orchestration-and-workflows.md#45-w5--investigate-issue-operations_analyst); [10 §5](10-data-model-and-storage.md#5-traceability--roadmap-entities-fr-c6-c7) | S1–S3; AC5–AC7 |
| R6 | No hallucination; resolvable citation for every claim/classification/field; block unverifiable; mechanical Verifier; auto-redact secrets/PII | [06](06-grounding-verifier-layer.md) V0–V10, bounded judge, SDP; [10 §2.3](10-data-model-and-storage.md#23-citation-and-claim--the-grounding-contract-r6) claim contract | E3 (uncited = 0 hard gate); E7; AC8 |

## 2. Definition of Done

| DONE | Statement | Design | Verification |
|---|---|---|---|
| 1 | Signals from ADO, ServiceNow, PagerDuty, Email, OneDrive auto-ingested, normalized, deduplicated into a Product Problem | W1 + W2; `signal-event-gateway`; correlation-engine | AC1; E1 |
| 2 | Missing information identified; clarifying questions drafted | `readiness_assessor` + `question_generator` | AC2; E2 |
| 3 | Requirement draft in SLB template with a citation on every populated field | `spec_writer` + V1–V6 | AC3; E3 |
| 4 | Prototype generated in Figma reusing SLB DLS components | `prototype_planner` + renderer (plugin path now; MCP path when admitted) — ADR-006 | AC4; E5 |
| 5 | No write actions (ADO, KB, Figma) without explicit PM approval | Action catalog; tokens; executor; Gateway CEL | AC9; E6 |
| 6 | Immutable, queryable audit ledger of every draft, approval, commit | [07 §8](07-hitl-approval-and-audit-ledger.md#8-ledger-design) | AC10 |
| 7 | Groundedness evals gate CI at zero uncited claims | [13 §3.1](13-testing-and-evaluation.md#31-how-the-zero-uncited-gate-is-computed); CI stage 5 | CI pipeline |
| 8 | Every tool call authorized through Agent Gateway with a down-scoped SPIFFE identity | [08 §3–4](08-security-identity-and-governance.md#3-identity-model); agents on Agent Runtime (ingress + egress supported) | Gateway policy tests; dry-run → enforce reports |
| 9 | Services/connectors on Cloud Run and GKE (standard GCP serverless + GKE) | [03 §2](03-system-architecture.md#2-planes-and-containers); [12 §2](12-deployment-and-operations.md#2-deployment-units) | Terraform plan; deploy pipeline |

## 3. Functional requirements

| FR | Component · workflow | Data | Verification |
|---|---|---|---|
| A1 Multi-source ingestion | `signal_ingestor` · W1 | `Signal` | Connector contract tests; S1–S5 |
| A2 Correlation & dedup | `problem_builder` · W2; V10 | `ProductProblem.linked_signals` | E1; AC1 |
| A3 Characterization (class, impact, customers, frequency — cited) | `problem_characterizer` + deterministic `aggregate` | `Claim(kind=classification/aggregate)` | E3; V6 |
| A4 Prior-art / duplicates | W3 `prior_art`; V10 | `DuplicateCandidate` | S5 |
| A5 Readiness gap analysis | `readiness_assessor` | `ReadinessAssessment` | E2; AC2 |
| A6 Clarifying questions (+ outreach on approval) | `question_generator`; `OUTREACH_SEND_INTERNAL` | `ClarifyingQuestion` | E2 |
| A7 PRD + ADO draft, SLB templates, cited | `spec_writer` | `RequirementDraft` | E3; E4 |
| A8 Feasibility pre-check | `feasibility_assessor` (advisory, P-06) | `FeasibilityAssessment` | E4 |
| A9 Technical implementation plan (after approval) | `impl_planner` | `ImplementationPlanDraft` | E4 |
| A10 HITL commit | Ledger + executor | `ProposedAction`, token | AC3; E6 |
| B1 Spec → experience | `experience_translator` | `PrototypeBrief` | S6 |
| B2 DLS-aligned prototypes | `spec_composer` + renderer | `PrototypeSpec` | E5 |
| B3 Reuse justification | V9 / P-07 | `new_patterns[].justification` | E5 |
| B4 Validation package | W4 | `ValidationPackage` | S6 |
| B5 Feedback loop | Figma `FILE_COMMENT` webhooks → signals → revisions | `ValidationPackage.feedback` | Component test |
| B6 No external sharing | No external-share tool exists; `PROTOTYPE_RELEASE_INTERNAL` only | — | E6 |
| C1 Cross-system correlation (scale) | `operations_analyst` + correlation-engine | `InvestigationPack.scale` | S1, S2 |
| C2 Investigation assist + who to engage | W5 `assemble_pack`, `engagement_advisor` | `InvestigationPack` | S1–S3 |
| C3 Outcome classification | `outcome_classifier` + PM confirm | `InvestigationPack.outcome` | S2 |
| C4 Knowledge drafting, publish after Product-team approval | Sub-workflow K; `KB_PUBLISH_ARTICLE` | `KnowledgeArticleDraft` | AC6 |
| C5 Knowledge reuse at intake | W5 `kb_first` | — | AC5 |
| C6 Roadmap traceability chain | Sub-workflow T | `TraceLink` | AC7 |
| C7 Trends & opportunities (advisory) | Sub-workflow R | trend briefs | Rubric review |
| C8 Continuously updated problem view | Background problem builder; Problem Inbox | `ProductProblem` | Freshness SLO |

## 4. Governance, NFR, integration

| ID | Control | Where |
|---|---|---|
| GOV1 | Draft-then-approve | [07](07-hitl-approval-and-audit-ledger.md) |
| GOV2 | Evidence rule | [06](06-grounding-verifier-layer.md) V1–V6 |
| GOV3 | Advice vs decision | P-06; roadmap decisions are PM acts |
| GOV4 | Redaction | [06 §5](06-grounding-verifier-layer.md#5-redaction-pii--secrets) |
| GOV5 | Prohibited actions | P-02/P-03; closed catalog |
| GOV6 | Auditability | Ledger + Gateway telemetry |
| GOV7 | Content is data | [06 §8](06-grounding-verifier-layer.md#8-prompt-injection--content-as-data-gov7); [08 §8](08-security-identity-and-governance.md#8-threat-model-stride--agent-specific) T1 |
| NFR-1 | Least privilege, vault, no standing writes | [08 §3–4](08-security-identity-and-governance.md#3-identity-model) |
| NFR-2 | Residency / VPC-SC day one | [03 §5](03-system-architecture.md#5-network--perimeter) |
| NFR-3 | Enterprise LLM governance | [08 §7](08-security-identity-and-governance.md#7-model-governance-nfr-3) |
| NFR-4 | 100 % cited approved claims | E3 gate |
| NFR-5 | Immutable, queryable audit | [07 §8](07-hitl-approval-and-audit-ledger.md#8-ledger-design) |
| NFR-6 | Interactive latency | [05 §8](05-orchestration-and-workflows.md#8-latency-budget-nfr-6-near-interactive-single-problem) |
| NFR-7 | Scalability | Agent Runtime/Cloud Run autoscaling; GKE HPA; AlloyDB read pools |
| NFR-8 | Graceful degradation, no partial output | [03 §6](03-system-architecture.md#6-resilience--degradation-nfr-8-ir3-requirements-13); fail-closed verifier |
| NFR-9 | Configurable without code | DM-7 `config.*` |
| NFR-10 | Observability & KPI dashboards | [12 §5](12-deployment-and-operations.md#5-observability) |
| NFR-11 | Legal tags, confidentiality | P-04/P-05; `Signal.sensitivity` |
| IR1 | Least-privilege connector identities | read/write server split |
| IR2 | Immutable provenance | `Signal.provenance_hash`, permalinks |
| IR3 | Graceful connector failure | `sources_unavailable`; `SourceHealth` |
| IR4 | Backoff; payloads by handle | Connector contract; `ArtifactHandle` |

## 5. Acceptance criteria

AC1–AC10 → tests: see [13 §4](13-testing-and-evaluation.md#4-acceptance-criteria--tests).

## 6. Required services coverage

All eleven required services are used for their stated purpose — see [03 §7](03-system-architecture.md#7-required-services--where-they-are-used).
