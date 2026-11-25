# 15 — Risks, Assumptions & Open Questions

---

## 1. Open questions for SLB (prioritized)

| ID | Question | Why it matters | Blocks | Owner (proposed) |
|---|---|---|---|---|
| **Q-01** | Which system is the **SLB Knowledge Base** (ServiceNow Knowledge, Confluence, SharePoint, internal)? API, publish workflow, reviewer group? | No public SLB KB API exists; `mcp-kb`/`mcp-kb-write` adapter target | M5 (AC6) | SLB Product Ops |
| **Q-02** | Figma plan tier and seats: Enterprise? Full seat for a service/bot user? Permission to install a **private org plugin**? OK to apply to the **Figma MCP Catalog** waitlist for our client? | Unattended canvas writes need an admitted MCP client + Full seat; plugin path needs org approval | M6 (DONE-4) | SLB UX lead |
| Q-03 | SLB DLS library: file key(s), publish cadence, custom fonts, image-based components | `use_figma` lacks custom-font/image support; catalog sync | M6 | SLB UX lead |
| **Q-04** | ADO process template: work item types (`Requirement`, `Initiative`, `Bug`), required/custom fields, area/iteration paths, title conventions; orgs/projects in scope (corpus shows `slb1-swt/Planck`) | Template mapping (`config.ado_field_map`), write-SP permissions | M3–M4 | SLB Engineering |
| Q-05 | ServiceNow scope: instance/release, tables (incident, sc_request, sc_req_item, CSM cases), integration user, outbound REST allowed? | Connector scope & hints | M1 | SLB ITSM |
| Q-06 | Is SLB on Figma **Enterprise** (variables REST API)? | Token/variable-aware specs | M6 | SLB UX lead |
| Q-07 | PagerDuty: account/region, services in scope, API key vs OAuth app | Connector auth | M1 | SLB SRE |
| **Q-08** | Workforce IdP (Entra ID?) for IAP/Workforce Identity Federation; approver group names per product line and for KB review | Approver identity binding | M2/M4 | SLB IAM |
| **Q-09** | Residency & retention: may any model call use `us`/`global` endpoints (e.g. `gemini-embedding-2`)? Zero-data-retention requirement? Retention periods? Policy for employee names in drafts? | NFR-2/3/11, redaction policy | M0 | SLB Security & Legal |
| **Q-10** | GCP organization ID and projects for stage/prod; Shared VPC; billing | Agent Identity principals are org-scoped; Terraform envs | M0 | SLB Cloud team |
| **Q-11** | Approval to derive sanitized eval fixtures from the shared corpus | Golden scenarios S1–S7 | M1 | SLB Product lead |
| **Q-12** | Pilot product line(s) and pilot PMs; KPI baselines (§3.3) | G0 gap; success measurement | M0 | SLB Product lead |
| Q-13 | Mailboxes and OneDrive/SharePoint sites in scope; Teams in v1 or extension? | Graph RBAC scope, `Sites.Selected` | M1 | SLB IAM / Product |
| Q-14 | Internal outreach send in v1 (shared mailbox name) or drafts only? | `OUTREACH_SEND_INTERNAL` | M3 | SLB Product lead |
| Q-15 | Where do architecture/feasibility references live (docs, ADO wiki, repos)? | `feasibility_assessor` evidence | M3 | SLB Architecture |

## 2. Assumptions

| # | Assumption | If false |
|---|---|---|
| A-1 | SLB provides non-prod access to ADO, ServiceNow, PagerDuty, Graph, Figma, KB with least-privilege identities (requirements §15.1) | R1 delayed; fixture-only development continues |
| A-2 | Gemini Enterprise Agent Platform in `us-central1` is an approved enterprise LLM endpoint for SLB data | Re-plan model endpoints (Q-09) |
| A-3 | SLB templates in the corpus (`In-depth Requirement Description`, `Work Items List`) reflect current standards | Template config change only (NFR-9) |
| A-4 | PMs will act on approvals within 72 h (proposal TTL) | TTL configurable |
| A-5 | A GCP organization exists (Agent Identity trust domain is org-scoped) | Blocks Agent Identity (Q-10) |

## 3. Risk register

| ID | Risk | L | I | Mitigation | Trigger / owner |
|---|---|---|---|---|---|
| RK-01 | **Figma does not support unattended generation by custom clients** (MCP Catalog allow-list; per-user OAuth) | H | M | Primary renderer = private plugin run on approval (path A); apply for Catalog admission (path B); same compiled program for both (ADR-006) | Q-02 answer / UX lead |
| RK-02 | **A2UI pre-1.0 churn** (SDK breaking changes per release; v1.0 RC) | H | M | Exact pins; `pm_agent.a2ui` adapter; contract tests; scheduled migration to v1.0 when renderers ship | Release notes / Tech lead |
| RK-03 | Preview features (Cloud Run IAP MCP, Semantic Governance, Agent Identity API, Runtime revisions) change | M | M | Not on the critical control path (IAM UAP + tokens are GA-based); fallbacks in SP-3 | Release notes |
| RK-04 | Agent Gateway governs only Agent Runtime / Gemini Enterprise sources | — (designed for) | H | All agents on Agent Runtime; Cloud Run/GKE only as destinations | — |
| RK-05 | Model availability/residency on `us-central1` | M | M | SP-2; pinned ids; multi-region fallback with sign-off | SP-2 |
| RK-06 | Weak correlation on fragmented/low-quality data | M | H | Hard cross-ref links; adjudication; PM confirmation; E1 tuning; feedback learning | E1 trend |
| RK-07 | **Over-strict verifier** blocks too much → PM frustration | M | M | Better evidence packs; clear "why blocked" + question; monitor blocked-claim rate; tune V5 prompts (never relax V1–V4) | `claims_blocked` metric |
| RK-08 | SLB security rejects the narrow ADO write grant | M | H | Draft-only fallback: approved payload exported for manual creation; ledger still records approval (requirements §15.3) | Security review |
| RK-09 | SLB KB backend unknown/late | M | M | Adapter; knowledge drafts usable as ADO wiki/SharePoint pages meanwhile | Q-01 |
| RK-10 | Vendor rate limits (ADO TSTU, Graph throttling, Figma MCP daily caps) | M | M | Delta queries, backoff, caching, batch windows, Enterprise seats | Connector metrics |
| RK-11 | Legal tags / customer confidentiality mis-handled | L | H | P-04/P-05 policies, SDP, two-person rule for global KB, legal review in G4 | Legal |
| RK-12 | Adoption/trust | M | H | Strict HITL, visible citations, partial-data banners, champion-led pilot | KPI dashboard |
| RK-13 | Cost overrun | M | M | Model routing, caching, budgets, Batch API | Budget alerts |
| RK-14 | Rapid ADK releases (bi-weekly) | H | L | Pin minor; upgrade via ADR + full eval run | Release notes |
| RK-15 | Third-party OAuth via Agent Identity Auth Manager under-documented | M | L | Secret Manager per-server credentials (SP-9) | SP-9 |

## 4. Unconfirmed platform items

Tracked with sources and spikes in [02](02-platform-verification-log.md) and [14 §4](14-implementation-roadmap.md#4-spikes-for-unconfirmed-items). None of them sits on the R2 or R6 critical control path: HITL enforcement relies on GA identity/IAM features plus our own tokens and executor; grounding relies on deterministic code we own.
