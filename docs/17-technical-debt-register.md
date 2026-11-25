# 17 — Technical Debt & Production-Hardening Register

> Created during the 2026-11-24 L6 code-health review. Items below were verified against the codebase and **deliberately deferred** from the offline reference-implementation refactor because they require SLB vendor sandboxes, multi-replica production infrastructure (Q-01…Q-15), or cross-team schema/UI design sign-off. Each entry names its exact location, failure mode, minimal target design, and unblocking milestone.

---

## P0 — Must land before stage/prod cutover (Q-10 / M1–M3)

| ID | Area | Location | Verified failure mode | Minimal target design | Unblocks at |
|---|---|---|---|---|---|
| **TD-01** | Delta cursors | `connectors/servicenow.py:146,266`, `connectors/pagerduty.py:213`, `connectors/ado.py:145,320` | ServiceNow & PagerDuty poll with `>=` and `offset=0`: if more rows than `limit` share a 1-second timestamp (bulk import), the cursor stalls forever. ADO takes its cursor from the batch read rather than the WIQL `asOf` snapshot, so concurrent edits can skip records. | Shared `Watermark(ts, offset_or_last_id)` in `connectors/common.py`; deterministic `ORDER BY ts, id`; ADO passes WIQL `asOf` to `workitemsbatch`; PagerDuty pins `sort_by=created_at:asc`. Accept legacy timestamp cursor once for migration. | M1 (vendor sandboxes Q-04/05/07) |
| **TD-02** | Child & library pagination | `connectors/ado.py:340`, `servicenow.py:172`, `pagerduty.py:253`, `figma.py:248,291` | Comments, journal entries, log entries, and Figma team components/sets read only the first page (silent truncation, violates IR3). | Shared bounded `collect_pages(first, next_from, items_from, max_items)` returning `(items, truncated)` so truncation surfaces as partial-data status rather than silent omission. | M1 (vendor sandboxes) |
| **TD-03** | BFF pause registry | `workbench/app.py:91-96,183-189` | `_State.pauses` and `action_interrupts` live in Cloud Run process memory; approvals may span days (docs/05), so a deploy or second replica causes `/decide` to record `APPROVED` + mint a token without resuming the paused ADK workflow. | Persist `session_id` + `user_id` on `ProposedAction` during `propose` (fields already exist), then locate the interrupt via Agent Platform Sessions + `pending_interrupts()` matching `payload.action_id`. | M2 (Agent Runtime + Sessions, Q-10) |
| **TD-04** | Multi-instance storage & executor ports | `hitl/approvals.py:96-100`, `hitl/executor.py:63`, `executor_app/__init__.py` | `ApprovalService` keeps state in five in-process dicts (tokens keyed by `action_id`), and `CommitExecutor` depends on concrete `ApprovalService`/`Verifier` rather than Protocols, so production `commit-executor` cannot call remote `mcp-ledger`/`mcp-verifier`. | Extract `ApprovalRepository` (`unit_of_work()`, tokens keyed by `jti`) backed by AlloyDB `hitl.*` tables, and `LedgerPort`/`VerifierPort` Protocols in `hitl/executor.py` with in-process and `ToolGateway` adapters. | M2 (AlloyDB, Q-10) |
| **TD-05** | Durable `JtiRegistry` | `hitl/write_guard.py`, `serve.py:_kms_guard` | `_kms_guard` now fails closed in strict mode until a cross-instance registry is wired. | Implement `JtiRegistry` over Memorystore (`SET NX EX`) or AlloyDB (`INSERT … ON CONFLICT DO NOTHING`) and inject it in `serve.py`. | M2 (Q-10) |

---

## P1 — Design debt to schedule in M1–M4

| ID | Area | Location | Verified gap | Minimal target design |
|---|---|---|---|---|
| **TD-06** | Partial-poll isolation | `connectors/ado.py`, `servicenow.py`, `pagerduty.py`, `msgraph.py:399` | One malformed record or a `.xlsx` without a `"Tracker"` sheet aborts the entire page; `PollResult.status`/`reason` are never set to partial. | Shared `normalize_page()` isolating per-record errors, advancing cursor only to the last contiguous processed record, and emitting `PARTIAL` status. |
| **TD-07** | Transport retry / backoff | `connectors/http.py` | `docs/09 §1` specifies `Retry-After` handling + jittered exponential backoff; currently `retry_after_seconds` is parsed and surfaced in `ConnectorError` but not retried. | Add an `idempotent: bool` flag on `HttpRequest` and bounded retry loop in `HttpxTransport` for idempotent GET/WIQL reads only (never mutating writes). |
| **TD-08** | Duplicate-decision A2UI path & BFF interrupt binding | `workbench/app.py:78,183-189,229`, `workflows/define_requirement.py:250` | Per-candidate duplicate events (`confirm_merge`/`link_duplicate`/`mark_distinct`) return 422 from the BFF; `_decide` accepts a client-supplied `interrupt_id` without verifying it belongs to `action_id`; invalid `DuplicateDecision` strings can wedge resume. | Route duplicate events in the BFF, verify `pause.payload["action_id"] == action_id`, and validate decision enum values safely inside `apply_duplicate_decision`. |
| **TD-09** | Approvals transition table & read-back wiring | `hitl/approvals.py`, `hitl/executor.py:65,147`, `workflows/define_requirement.py:276` | State transitions are checked inline rather than against a single `_TRANSITIONS` map (`CANCELLED` unused; `FAILED → PENDING_APPROVAL` in `docs/07` not implemented). `CommitExecutor.read_back` defaults to `None`. `_on_status` ignores `changes_requested`/`expired`/`failed`. | Centralize `_TRANSITIONS` in `approvals.py`, align `docs/07`, wire per-`ActionSpec` read-back verifiers once read connectors exist, and map every `CommitOutcome` in `_on_status`. |
| **TD-10** | Webhook hint → `get()` contract | `event_gateway/app.py:314,328`, `connectors/msgraph.py:344`, `figma.py:294` | Graph hints carry a `resource` path while `get()` expects a message id in mailbox `[0]`; Figma hints carry `file_key` while `get()` matches comment ids; Graph connectors read only `[0]` of configured mailbox/drive lists. | Define per-connector `resolve_hint(hint)` and add a gateway → connector contract test before implementing the Pub/Sub hint consumer. |
| **TD-11** | Typed MCP output schemas | `mcp_servers/tools.py` | Tool functions return `dict[str, Any]`, so MCP 2.x publishes an open `outputSchema`. | Return `TypedDict`s (`PollPage`, `SignalPage`, `SearchHits`) and pin `outputSchema` in `tests/contract/test_mcp_servers.py`. |
| **TD-12** | Taxonomy single source of truth | `schemas/evidence.py:51`, `schemas/problems.py:18`, `agents/llm_schemas.py:80` | `SignalTypeHint`, `ProblemClass`, and `ProblemOut.problem_class` duplicate the same five-value taxonomy, and `upsert_problem` discards `raw.problem_class`. | Unify on `ProblemClass` in `schemas/taxonomy.py` (`signal_type_hint: ProblemClass | None` mapping `"unknown"` → `None`) before stage data is persisted. |
| **TD-13** | Workflow idempotency keys | `workflows/define_requirement.py:157`, `workflows/approval.py:64` | `record_pm_answers` keys signals on fresh ledger entry ids; `propose` can create duplicate pending actions on node retry. | Key `pm_input` signals on `(problem_id, question_id, sha256(answer))` and make `approvals.propose` idempotent on `(draft_ref, report_id)`. |

---

## P2 — Opportunistic hygiene

* **Ledger anchor atomicity (`jobs/ledger_anchor.py:31-32`)**: return the verified head hash directly from `Ledger.verify_chain()` so anchoring cannot race a concurrent append.
* **Ledger query indexing (`hitl/ledger.py:173`)**: add `LedgerFilter` + store-level `select(filter, limit)` when moving from `InMemoryLedgerStore` to AlloyDB (avoids `O(N)` full scans).
* **ServiceNow webhook replay window (`event_gateway/verify.py:56`)**: add a signed timestamp header to the project's own ServiceNow HMAC contract before onboarding the vendor hook.
* **Redactor false positives & pseudonym width (`grounding/redaction.py:30,81`)**: tighten phone regex to avoid space-separated numeric series (e.g. `"depth 1200 1350 1400 ft"`), cover E.164 (`+15551234567`), and widen pseudonym hashes from 24 to 48+ bits before large corpora.
* **`a2ui/surfaces.py` modularization**: if more surfaces are added, split into `a2ui/surfaces/` (`_blocks.py`, `review.py`, `problems.py`, `ops.py`) re-exported from `__init__.py` (currently 10/10 pylint and all functions ≤ CC 13 after decomposing `problem_detail_surface` and `problem_inbox_surface`).
* **P-07 DLS rule single owner**: consolidate `verifier.py:_prototype_rules` and `policy.py:_dls_reuse` into `PolicyEngine`.
* **`GroundingGuard` private sentinel (`grounding/plugin.py:78`)**: avoid trusting a caller-supplied `"content_is_data"` key in tool outputs.
* **`workflows/ask.py` component test**: currently 51% covered (only `review_problems` is exercised); add a component test for `ask` with `ScriptedModelProvider`.
