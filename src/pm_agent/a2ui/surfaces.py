"""Deterministic A2UI v1.0 surface builders for the PM Workbench (docs/11 §3, §5, §7).

Pure functions over **already-verified** objects: no LLM lays out, omits or re-words decision content, and a
decision surface refuses to render unless the action, its report and its subject are exactly the verified,
pending triple (H-4 "approve exactly what was shown").

Every builder returns ``[createSurface]`` — one self-contained v1.0 message with the component tree and the data
model inline. Layout uses the basic catalog (surface default); domain components carry
``"@catalogId": "slb.com:pm_workbench_v1"``. Surface ids are deterministic UAX #31 identifiers derived from entity
ids (``approve_<action_id>``, ``req_review_<requirement_id>_v<version>``, …). Creating a surface whose id already
exists is an ``INTEGRITY_ERROR`` in v1.0, so re-render with :func:`delete_surface` first (or a new entity version).
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from typing import Any, Final

from pydantic import BaseModel
from pydantic_core import to_jsonable_python

from pm_agent.a2ui import _views as views
from pm_agent.a2ui._core import A2UI_VERSION, sanitize_id
from pm_agent.a2ui._ui import SurfaceBuilder, bind, bold, heading, label, md
from pm_agent.a2ui.actions import (
    ANSWER_QUESTIONS,
    APPROVE_ACTION,
    CONFIRM_MERGE,
    CONFIRM_OUTCOME,
    DRAFT_BUG,
    DRAFT_KB,
    DRAFT_OUTREACH,
    EDIT_FIELD,
    EXPORT,
    FILTER,
    INVESTIGATE,
    LINK_DUPLICATE,
    MARK_DISTINCT,
    OPEN_PROBLEM,
    REJECT_ACTION,
    REQUEST_CHANGES,
    SPLIT_SIGNAL,
    START_REQUIREMENT,
)
from pm_agent.grounding.verifier import subject_digest
from pm_agent.schemas import (
    ActionStatus,
    ActionType,
    ClarifyingQuestion,
    Confidence,
    DuplicateCandidate,
    DuplicateDecision,
    InvestigationPack,
    KnowledgeArticleDraft,
    LedgerEntry,
    LedgerEntryType,
    Outcome,
    ProblemClass,
    ProblemStatus,
    ProductProblem,
    ProposedAction,
    PrototypeSpec,
    QuestionStatus,
    ReadinessAssessment,
    ReadinessStatus,
    RequirementDraft,
    Signal,
    VerificationOutcome,
    VerificationReport,
    content_sha256,
)

MISSING_TEXT: Final = "missing — see clarifying question"
INFERRED_TEXT: Final = "⚠ Inferred — needs your confirmation"
ACTION_LABELS: Final[dict[ActionType, str]] = {
    ActionType.ADO_CREATE_WORK_ITEM: "Create ADO work item",
    ActionType.ADO_ADD_LINKS: "Add ADO links",
    ActionType.KB_PUBLISH_ARTICLE: "Publish knowledge article",
    ActionType.FIGMA_RENDER_PROTOTYPE: "Render Figma prototype",
    ActionType.FIGMA_POST_COMMENT: "Post Figma comment",
    ActionType.PROTOTYPE_RELEASE_INTERNAL: "Release prototype internally",
    ActionType.OUTREACH_SEND_INTERNAL: "Send internal outreach email",
    ActionType.ROADMAP_TRACE_LINK: "Record roadmap trace link",
}
HEALTHY_STATUSES: Final = frozenset({"ok", "healthy", "available", "up", "green"})
DEFAULT_MIN_REUSE_RATIO: Final = 0.9  # config policies.min_dls_reuse_ratio (FR-B3)

_PAYLOAD_COLUMNS = (("field", "Field"), ("value", "Value"))
_SIGNAL_COLUMNS = (("signal_id", "Signal"), ("source_system", "Source"), ("record_id", "Record"),
                   ("record_type", "Type"), ("captured_utc", "Captured (UTC)"), ("link_type", "Link"),
                   ("score", "Score"), ("permalink", "Permalink"))
_INBOX_COLUMNS = (("problem_id", "Problem"), ("title", "Title"), ("classification", "Classification"),
                  ("confidence", "Confidence"), ("status", "Status"), ("signals", "Signals"),
                  ("customers", "Affected customers"), ("open_questions", "Open questions"),
                  ("partial_sources", "Unavailable sources"))
_MAPPING_COLUMNS = (("screen", "Screen"), ("path", "Node"), ("kind", "Kind"), ("component_key", "DLS component"),
                    ("variant_props", "Variant"), ("text_claim_id", "Copy from claim"))
_NEAR_DUPLICATE_COLUMNS = views.DUPLICATE_COLUMNS
_RELATED_COLUMNS = (("signal_id", "Signal"), ("link_type", "Link"), ("score", "Score"), ("linked_by", "Linked by"))
_AUDIT_COLUMNS = (("seq", "Seq"), ("recorded_utc", "Recorded (UTC)"), ("entry_type", "Entry"), ("actor", "Actor"),
                  ("subjects", "Subjects"), ("entry_hash", "Entry hash"), ("prev_hash", "Previous hash"),
                  ("trace_id", "Trace"))

EditContext = Callable[[views.Section], dict[str, Any] | None]


# ------------------------------------------------------------------------------------------- guards
def _check_report_subject(report: VerificationReport, subject: BaseModel) -> None:
    if report.subject_sha256 != subject_digest(subject):
        raise ValueError(f"report {report.report_id} verified different content than this "
                         f"{type(subject).__name__} (subject_sha256 mismatch) — re-verify before showing it")


def _check_decision(action: ProposedAction, subject: BaseModel, report: VerificationReport) -> str:
    """Refuse decision surfaces for anything but the verified, pending action; return its payload hash."""
    digest = content_sha256(action.payload)
    problems = []
    if action.status is not ActionStatus.PENDING_APPROVAL:
        problems.append(f"action is {action.status.value}, not PENDING_APPROVAL")
    if action.payload_sha256 != digest:
        problems.append("payload_sha256 does not match the payload")
    if report.report_id != action.verification_report_id:
        problems.append(f"report {report.report_id} does not back the action (expects "
                        f"{action.verification_report_id})")
    if report.overall is not VerificationOutcome.PASS:
        problems.append(f"report outcome is {report.overall.value}, not PASS")
    if not report.commit_bound:
        problems.append("report is not commit-bound")
    if report.target_action != action.action_type.value:
        problems.append(f"report was issued for {report.target_action}, not {action.action_type.value}")
    if report.target_payload_sha256 != digest:
        problems.append("report did not verify this payload")
    if report.subject_sha256 != subject_digest(subject):
        problems.append("subject differs from the verified subject")
    if problems:
        raise ValueError(f"refusing to build a decision surface for {action.action_id}: " + "; ".join(problems))
    return digest


# ---------------------------------------------------------------------------------- shared blocks
def _verifier_block(sb: SurfaceBuilder, report: VerificationReport) -> list[str]:
    """VerifierBadge plus literal text of the outcome, every check status and the blocked-claim explanation."""
    sb.data["report"] = report.model_dump(mode="json")
    blocked = report.blocked_claim_ids
    ids = [sb.slb("verifier_badge", "VerifierBadge", a11y=f"Verifier result {report.overall.value}",
                  report=bind("report")),
           sb.text("verifier_summary", md(f"Verifier report {report.report_id}: {report.overall.value} · "
                                          f"Blocked claims: {len(blocked)}")),
           sb.text("verifier_checks", md("Checks: " + " · ".join(f"{c.check_id} {c.status.value}"
                                                               for c in report.checks)), caption=True)]
    if blocked:
        ids.append(sb.text("verifier_gate", md(f"⚠ {len(blocked)} claim(s) removed — no supporting evidence: "
                                               f"{', '.join(blocked)}. The clarifying questions replace them."),
                           live="polite"))
    return ids


def _section_blocks(sb: SurfaceBuilder, sections: Sequence[views.Section], *, prefix: str = "sec",
                    path: str = "sections", edit: EditContext | None = None) -> list[str]:
    """One block per drafted field: label + ClaimList (claims and their citation chips) or the missing marker."""
    ids = []
    for index, sec in enumerate(sections):
        cid = f"{prefix}_{index}"
        status = str(sec["status"])
        children = [sb.text(f"{cid}_label", f"{bold(str(sec['label']))} · {md(status)}")]
        if sec["claims"]:
            children.append(sb.slb(f"{cid}_claims", "ClaimList", a11y=f"{sec['label']} — claims and citations",
                                   claims=bind(path, index, "claims"), fieldStatus=bind(path, index, "status"),
                                   label=str(sec["label"])))
        else:
            children.append(sb.text(f"{cid}_missing", md(MISSING_TEXT)))
        if any(claim["kind"] == "inference" for claim in sec["claims"]):
            children.append(sb.text(f"{cid}_inferred", md(INFERRED_TEXT), live="polite"))
        context = edit(sec) if edit else None
        if context is not None:
            children.append(sb.button(f"{cid}_edit", f"Edit {sec['label']}", EDIT_FIELD, context,
                                      a11y=f"Edit {sec['label']}", borderless=True))
        ids.append(sb.column(cid, children))
    return ids


def _normalise_diff(diff: Mapping[str, Any] | None) -> dict[str, Any]:
    """``{"before", "after"}`` as given, or per-field ``{field: {"before", "after"}}`` folded into two objects."""
    if not diff:
        return {}
    if set(diff) <= {"before", "after"}:
        return {"before": to_jsonable_python(diff.get("before")), "after": to_jsonable_python(diff.get("after"))}
    before: dict[str, Any] = {}
    after: dict[str, Any] = {}
    for field, change in diff.items():
        if not isinstance(change, Mapping) or not change or not set(change) <= {"before", "after"}:
            raise ValueError(f"diff[{field!r}] must be {{'before': …, 'after': …}}")
        before[str(field)] = to_jsonable_python(change.get("before"))
        after[str(field)] = to_jsonable_python(change.get("after"))
    return {"before": before, "after": after}


def _decision_summary(sb: SurfaceBuilder, action: ProposedAction, report: VerificationReport, digest: str, *,
                      diff: Mapping[str, Any] | None, with_badge: bool) -> list[str]:
    """What will happen, exactly: action identity, target, payload (+ hash), verifier status, optional diff."""
    normalised = _normalise_diff(diff)
    sb.data.update({"action": action.model_dump(mode="json"), "report": report.model_dump(mode="json"),
                    "diff": normalised, "review": {"comment": ""},
                    "payload_table": views.table(_PAYLOAD_COLUMNS, views.flatten(action.payload))})
    expires = action.model_dump(mode="json")["expires_utc"] or "n/a"
    ids = [sb.text("decision_title", heading(f"Approve: {ACTION_LABELS[action.action_type]}")),
           sb.text("decision_action", md(f"Action {action.action_id} · {action.action_type.value} · "
                                         f"version {action.version} · {action.status.value}")),
           sb.text("decision_target", md("Target: " + json.dumps(to_jsonable_python(action.target), sort_keys=True,
                                                                 ensure_ascii=False))),
           sb.text("decision_hash", md(f"Payload SHA-256: {digest}")),
           sb.text("decision_meta", md(f"Draft {action.draft_ref} · risk flags: "
                                       f"{', '.join(action.risk_flags) or 'none'} · expires {expires}"),
                   caption=True),
           sb.slb("approval_card", "ApprovalCard", a11y=f"Proposed action {action.action_id}", action=bind("action"),
                  report=bind("report"), diff=bind("diff"))]
    if with_badge:
        ids += _verifier_block(sb, report)
    ids.append(sb.slb("payload_table", "DataTable", a11y="Payload to be written", columns=bind("payload_table",
                                                                                                "columns"),
                      rows=bind("payload_table", "rows"), caption=f"Exact payload · {digest}"))
    if normalised:
        ids.append(sb.slb("field_diff", "FieldDiff", a11y="Changes compared with the existing object",
                          before=bind("diff", "before"), after=bind("diff", "after")))
    return ids


def _decision_controls(sb: SurfaceBuilder, action: ProposedAction, report: VerificationReport,
                       digest: str) -> list[str]:
    """Comment + Approve / Request changes / Reject. The context names the action literally (never a path)."""
    def context() -> dict[str, Any]:
        return {"actionId": action.action_id, "actionVersion": action.version, "payloadSha256": digest,
                "reportId": report.report_id, "comment": bind("review", "comment")}

    def comment_required() -> list[dict[str, Any]]:
        return [{"condition": {"@call": "required", "args": {"value": bind("review", "comment")}},
                 "message": "Add a comment that explains the decision."}]

    kind, ident = action.action_type.value, action.action_id
    comment = sb.text_field("review_comment", "Comment", bind("review", "comment"), a11y="Decision comment",
                            long=True, placeholder="Required when requesting changes or rejecting")
    buttons = [
        sb.button("approve_btn", f"Approve: {ACTION_LABELS[action.action_type]}", APPROVE_ACTION, context(),
                  a11y=f"Approve {kind} {ident}", primary=True, user_message=f"Approved {kind} {ident}"),
        sb.button("changes_btn", "Request changes", REQUEST_CHANGES, context(), a11y=f"Request changes to {ident}",
                  user_message=f"Requested changes to {kind} {ident}", checks=comment_required()),
        sb.button("reject_btn", "Reject", REJECT_ACTION, context(), a11y=f"Reject {kind} {ident}", borderless=True,
                  user_message=f"Rejected {kind} {ident}", checks=comment_required()),
    ]
    return [comment, sb.row("decision_buttons", buttons)]


def _mapping_block(sb: SurfaceBuilder, spec: PrototypeSpec) -> list[str]:
    sb.data["mapping"] = views.table(_MAPPING_COLUMNS, views.mapping_rows(spec))
    return [sb.text("mapping_title", heading("DLS component mapping", 3)),
            sb.slb("mapping_table", "DataTable", a11y="DLS component mapping", columns=bind("mapping", "columns"),
                   rows=bind("mapping", "rows"))]


def _subject_block(sb: SurfaceBuilder, subject: BaseModel) -> list[str]:
    """Every verified claim of the subject (grouped), plus type-specific facts the PM approves with it."""
    sections = views.subject_sections(subject)
    sb.data["sections"] = sections
    version = getattr(subject, "version", None)
    ident = next((getattr(subject, f) for f in ("spec_id", "article_id", "requirement_id", "pack_id", "link_id",
                                               "problem_id") if isinstance(getattr(subject, f, None), str)), "")
    ids = [sb.text("subject_title", heading("What was verified", 3)),
           sb.text("subject_ref", md(f"{type(subject).__name__} {ident}" + (f" · v{version}" if version else "")),
                   caption=True)]
    ids += _section_blocks(sb, sections)
    if isinstance(subject, PrototypeSpec):
        ids += _mapping_block(sb, subject)
    if isinstance(subject, KnowledgeArticleDraft):
        ids.append(sb.text("subject_applies", md(f"Applies to {subject.applies_to_product} "
                                                 f"{', '.join(subject.applies_to_versions)} · audience "
                                                 f"{subject.audience.value}")))
    return ids


def _candidate_row(sb: SurfaceBuilder, cid: str, subject_id: str, candidate: DuplicateCandidate) -> str:
    """Duplicate candidate summary; pending ones get Merge / Link / Keep distinct with literal context."""
    def context() -> dict[str, Any]:
        return {"subjectId": subject_id, "candidateRef": candidate.ref, "candidateSystem": candidate.system}

    who = f"{candidate.system} {candidate.ref}"
    children = [sb.text(f"{cid}_text", md(f"{who} · similarity {candidate.similarity:.2f} ({candidate.method}) · "
                                          f"{candidate.decision.value}"))]
    if candidate.decision is DuplicateDecision.PENDING:
        children += [sb.button(f"{cid}_merge", "Merge", CONFIRM_MERGE, context(), a11y=f"Merge with {who}"),
                     sb.button(f"{cid}_link", "Link", LINK_DUPLICATE, context(), a11y=f"Link to {who}"),
                     sb.button(f"{cid}_distinct", "Keep distinct", MARK_DISTINCT, context(),
                               a11y=f"Keep distinct from {who}", borderless=True)]
    return sb.row(cid, children)


def _healthy(status: str) -> bool:
    return status.strip().lower() in HEALTHY_STATUSES


# ------------------------------------------------------------------------------------------ builders
def approval_surface(action: ProposedAction, subject: BaseModel, report: VerificationReport, *,
                     diff: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """Decision surface for one pending action — shows exactly what was verified and lets the PM decide.

    Shows ``action_id``, ``action_type``, target, payload (verbatim table) and ``payload_sha256``; the verifier
    ``report_id``, outcome, every check status and the blocked-claim count; every claim of *subject* with its
    citations; the optional *diff* (``{"before", "after"}`` or ``{field: {"before", "after"}}``). Approve /
    Request changes / Reject emit ``approve_action`` / ``request_changes`` / ``reject_action`` with context
    ``{actionId, actionVersion, payloadSha256, reportId, comment: @/review/comment}``.

    Raises ``ValueError`` unless *action* is PENDING_APPROVAL and *report* is the signed-off commit-bound PASS that
    verified this exact payload and this exact *subject*.
    """
    digest = _check_decision(action, subject, report)
    sb = SurfaceBuilder(f"approve_{action.action_id}")
    ids = _decision_summary(sb, action, report, digest, diff=diff, with_badge=True)
    ids += _subject_block(sb, subject)
    ids += _decision_controls(sb, action, report, digest)
    return sb.messages(ids)


def requirement_review_surface(draft: RequirementDraft, report: VerificationReport | None, *,
                               action: ProposedAction | None = None,
                               template_fields: Sequence[str] | None = None) -> list[dict[str, Any]]:
    """Requirement Review (FR-A7/A8/A10): one ClaimList per field, verifier badge, gate explanation, edits.

    *template_fields* (e.g. ``config.templates["slb-ado-requirement-v1"].fields``) fixes the field order and shows
    absent fields as "missing — see clarifying question". ``edit_field`` buttons carry
    ``{requirementId, version, field}``. Pass the pending *action* to append the decision block.
    """
    if report is not None:
        _check_report_subject(report, draft)
    digest = None
    if action is not None:
        if report is None:
            raise ValueError("a decision block needs the verification report that backs the action")
        digest = _check_decision(action, draft, report)
    sb = SurfaceBuilder(f"req_review_{draft.requirement_id}_v{draft.version}")
    sections = views.requirement_sections(draft, template_fields)
    sb.data.update({"draft": {"requirement_id": draft.requirement_id, "version": draft.version,
                              "problem_id": draft.problem_id, "template": draft.template,
                              "work_item_type": draft.work_item_type, "inferred_fields": draft.inferred_fields,
                              "approval_state": draft.approval_state.value},
                    "sections": sections})
    ids = [sb.text("title", heading("Requirement draft review")),
           sb.text("meta", md(f"{draft.requirement_id} · v{draft.version} · template {draft.template} · "
                              f"{draft.work_item_type} · problem {draft.problem_id} · "
                              f"approval {draft.approval_state.value}"), caption=True)]
    if report is None:
        ids.append(sb.text("unverified", md("Not verified yet — the Verifier has not checked this version."),
                           live="polite"))
    else:
        ids += _verifier_block(sb, report)
    if draft.inferred_fields:
        ids.append(sb.text("inferred_fields", md(f"{INFERRED_TEXT}: "
                                                 f"{', '.join(label(f) for f in draft.inferred_fields)}")))

    def edit(sec: views.Section) -> dict[str, Any] | None:
        if sec["key"] == "feasibility":  # advisory (GOV3): Engineering decides, the PM does not edit it
            return None
        return {"requirementId": draft.requirement_id, "version": draft.version, "field": sec["key"]}

    ids += _section_blocks(sb, sections, edit=edit)
    if draft.near_duplicates:
        sb.data["near_duplicates"] = views.table(_NEAR_DUPLICATE_COLUMNS,
                                                 [views.duplicate_row(c) for c in draft.near_duplicates])
        ids += [sb.text("near_duplicates_title", heading("Near duplicates (G3)", 3)),
                sb.slb("near_duplicates_table", "DataTable", a11y="Near-duplicate work items",
                       columns=bind("near_duplicates", "columns"), rows=bind("near_duplicates", "rows"))]
    if action is not None and report is not None and digest is not None:
        ids += _decision_summary(sb, action, report, digest, diff=None, with_badge=False)
        ids += _decision_controls(sb, action, report, digest)
    return sb.messages(ids)


def readiness_surface(problem_id: str, readiness: ReadinessAssessment,
                      questions: list[ClarifyingQuestion]) -> list[dict[str, Any]]:
    """Readiness & Questions (FR-A5/A6): checklist, an answer field per open question, outreach picks.

    ``answer_questions`` sends ``{problemId, answers: {question_id: text}}`` (answers become ``pm_input``
    signals); ``draft_outreach`` sends ``{problemId, respondents: {question_id: [person tokens]}}``.
    """
    open_questions = [q for q in questions if q.status is QuestionStatus.OPEN]
    sb = SurfaceBuilder(f"readiness_{problem_id}")
    sb.data.update({"problem_id": problem_id, "readiness": readiness.model_dump(mode="json"),
                    "questions": [q.model_dump(mode="json") for q in questions],
                    "answers": {q.question_id: "" for q in open_questions},
                    "outreach": {q.question_id: [] for q in open_questions if q.suggested_respondents}})
    present = sum(1 for item in readiness.items if item.status is ReadinessStatus.PRESENT)
    ids = [sb.text("title", heading("Readiness & clarifying questions")),
           sb.text("summary", md(f"{problem_id} · {readiness.checklist_id} · {present}/{len(readiness.items)} "
                                 f"present · {len(readiness.gaps)} gap(s) · {len(open_questions)} open question(s)")),
           sb.slb("checklist", "ReadinessChecklist", a11y="Readiness checklist", items=bind("readiness", "items"))]
    if questions:
        ids.append(sb.text("questions_title", heading("Clarifying questions", 3)))
    for index, question in enumerate(questions):
        ids.append(_question_card(sb, index, question))
    if open_questions:
        buttons = [sb.button("answer_btn", "Submit answers", ANSWER_QUESTIONS,
                             {"problemId": problem_id, "answers": bind("answers")},
                             a11y="Submit answers to the clarifying questions", primary=True,
                             user_message=f"Answered clarifying questions for {problem_id}")]
        if sb.data["outreach"]:
            buttons.append(sb.button("outreach_btn", "Draft outreach", DRAFT_OUTREACH,
                                     {"problemId": problem_id, "respondents": bind("outreach")},
                                     a11y="Draft outreach to the selected people"))
        ids.append(sb.row("question_actions", buttons))
    return sb.messages(ids)


def _question_card(sb: SurfaceBuilder, index: int, question: ClarifyingQuestion) -> str:
    cid = f"q_{index}"
    children = [sb.text(f"{cid}_text", f"{bold(label(question.checklist_item.value))} · {md(question.text)}")]
    if question.status is not QuestionStatus.OPEN:
        answer = f" · answer {question.answer_signal_id}" if question.answer_signal_id else ""
        children.append(sb.text(f"{cid}_status", md(f"Status: {question.status.value}{answer}"), caption=True))
    if question.why_asked:
        children.append(sb.slb(f"{cid}_why", "ClaimList", a11y="Why this question is asked",
                               claims=bind("questions", index, "why_asked"), label="Why we ask"))
    if question.status is QuestionStatus.OPEN:
        children.append(sb.text_field(f"{cid}_answer", "Your answer", bind("answers", question.question_id),
                                      a11y=f"Answer: {' '.join(question.text.split())[:120]}", long=True))
        if question.suggested_respondents:
            options = [(f"{r.person.token} ({r.person.role_hint})" if r.person.role_hint else r.person.token,
                        r.person.token) for r in question.suggested_respondents]
            children.append(sb.choice_picker(f"{cid}_who", "Ask for input", options,
                                             bind("outreach", question.question_id), a11y="People to ask",
                                             multiple=True))
    return sb.card(cid, sb.column(f"{cid}_body", children))


def duplicate_decision_surface(subject_id: str, candidates: list[DuplicateCandidate]) -> list[dict[str, Any]]:
    """Duplicate decision: candidates table; Merge / Link / Keep distinct per *pending* candidate.

    Events ``confirm_merge`` / ``link_duplicate`` / ``mark_distinct`` carry ``{subjectId, candidateRef,
    candidateSystem}`` as literals.
    """
    sb = SurfaceBuilder(f"duplicates_{subject_id}")
    sb.data.update({"subject_id": subject_id, "candidates": [c.model_dump(mode="json") for c in candidates],
                    "table": views.table(views.DUPLICATE_COLUMNS, [views.duplicate_row(c) for c in candidates])})
    pending = sum(1 for c in candidates if c.decision is DuplicateDecision.PENDING)
    ids = [sb.text("title", heading("Possible duplicates")),
           sb.text("summary", md(f"{subject_id} · {len(candidates)} candidate(s), {pending} pending · decide "
                                 "for each: merge, link or keep distinct")),
           sb.slb("candidates_table", "DataTable", a11y="Duplicate candidates", columns=bind("table", "columns"),
                  rows=bind("table", "rows"))]
    ids += [_candidate_row(sb, f"cand_{i}", subject_id, c) for i, c in enumerate(candidates)]
    return sb.messages(ids)


def problem_detail_surface(problem: ProductProblem, signals: list[Signal], *,
                           reveal_allowed: bool = False) -> list[dict[str, Any]]:
    """Problem Detail (FR-A2/A3, FR-C1): cited claims, linked signals, evidence excerpts with chips.

    Events: ``start_requirement`` / ``investigate`` ``{problemId}``; ``confirm_merge`` / ``link_duplicate`` /
    ``mark_distinct`` per pending duplicate; ``split_signal`` ``{problemId, signalIds: @/split/signal_ids}``.
    *reveal_allowed* only toggles the EvidencePanel re-identification affordance (enforced by the BFF).
    """
    by_id = {s.signal_id: s for s in signals}
    sections = views.problem_sections(problem)
    claims = [claim for _, _, group in views.problem_claim_groups(problem) for claim in group]
    linked = {link.signal_id: link for link in problem.linked_signals}
    order = [*linked, *(s.signal_id for s in signals if s.signal_id not in linked)]
    rows: list[views.Row] = []
    for signal_id in order:
        signal, link = by_id.get(signal_id), linked.get(signal_id)
        rows.append({"signal_id": signal_id,
                     "source_system": signal.source_system.value if signal else None,
                     "record_id": signal.source_record_id if signal else None,
                     "record_type": signal.record_type.value if signal else None,
                     "captured_utc": signal.model_dump(mode="json")["captured_utc"] if signal else None,
                     "link_type": link.link_type.value if link else None,
                     "score": round(link.score, 4) if link else None,
                     "permalink": signal.permalink if signal else None})
    evidence = views.evidence_items(claims, by_id)
    sb = SurfaceBuilder(f"problem_{problem.problem_id}_v{problem.version}")
    sb.data.update({"problem": {"problem_id": problem.problem_id, "version": problem.version,
                                "status": problem.status.value, "product_line": problem.product_line,
                                "classification_confidence": problem.classification_confidence.value},
                    "sections": sections, "signals": views.table(_SIGNAL_COLUMNS, rows), "evidence": evidence,
                    "reveal_allowed": reveal_allowed, "split": {"signal_ids": []}})
    ids = [sb.text("title", heading(f"Problem: {problem.title.text}")),
           sb.text("meta", md(f"{problem.problem_id} · v{problem.version} · {problem.status.value} · "
                              f"{problem.classification.text} ({problem.classification_confidence.value} confidence)"
                              f" · product line {problem.product_line or 'n/a'}"), caption=True)]
    if problem.classification_confidence is Confidence.LOW:
        ids.append(sb.text("low_confidence", md("⚠ Low-confidence classification — needs your confirmation"),
                           live="polite"))
    if problem.sources_unavailable:
        sb.data["source_health"] = [{"source": s, "status": "unavailable", "healthy": False}
                                    for s in problem.sources_unavailable]
        ids += [sb.text("partial_data", md("⚠ Partial data — unavailable during the last refresh: "
                                           + ", ".join(problem.sources_unavailable)), live="assertive"),
                sb.slb("source_health", "SourceHealth", a11y="Source health", live="polite",
                       sources=bind("source_health"))]

    overview = _section_blocks(sb, sections)
    if problem.duplicates:
        overview.append(sb.text("duplicates_title", heading("Possible duplicates", 3)))
        overview += [_candidate_row(sb, f"dup_{i}", problem.problem_id, c) for i, c in enumerate(problem.duplicates)]
    overview.append(sb.row("problem_actions", [
        sb.button("start_requirement_btn", "Start requirement", START_REQUIREMENT, {"problemId": problem.problem_id},
                  a11y="Start a requirement draft for this problem", primary=True),
        sb.button("investigate_btn", "Investigate", INVESTIGATE, {"problemId": problem.problem_id},
                  a11y="Open an investigation pack for this problem")]))

    signal_children = [sb.slb("signals_table", "DataTable", a11y="Linked signals", columns=bind("signals", "columns"),
                              rows=bind("signals", "rows"))]
    if problem.linked_signals:
        options = [(f"{by_id[i].source_system.value} {by_id[i].source_record_id}" if i in by_id else i, i)
                   for i in linked]
        signal_children.append(sb.row("split_row", [
            sb.choice_picker("split_picker", "Signals to split off", options, bind("split", "signal_ids"),
                             a11y="Signals to split into another problem", multiple=True),
            sb.button("split_btn", "Split signal", SPLIT_SIGNAL,
                      {"problemId": problem.problem_id, "signalIds": bind("split", "signal_ids")},
                      a11y="Split the selected signals into another problem")]))

    evidence_children = []
    for index, item in enumerate(evidence):
        cite, cid = item["citation"], f"ev_{index}"
        chip = sb.slb(f"{cid}_chip", "CitationChip", a11y=f"Citation {cite['evidence_id']}",
                      evidenceId=bind("evidence", index, "citation", "evidence_id"),
                      sourceSystem=bind("evidence", index, "citation", "source_system"),
                      recordId=bind("evidence", index, "citation", "source_record_id"),
                      permalink=bind("evidence", index, "citation", "permalink"),
                      quote=bind("evidence", index, "citation", "quote"))
        panel = sb.slb(f"{cid}_panel", "EvidencePanel",
                       a11y=f"Evidence from {cite['source_system']} {cite['source_record_id']}",
                       quote=bind("evidence", index, "citation", "quote"),
                       span=bind("evidence", index, "citation", "span"), signal=bind("evidence", index, "signal"),
                       revealAllowed=bind("reveal_allowed"))
        evidence_children.append(sb.card(cid, sb.column(f"{cid}_body", [chip, panel])))
    if not evidence_children:
        evidence_children.append(sb.text("no_evidence", md("No citations on this problem yet.")))

    ids.append(sb.tabs("problem_tabs", [("Overview", sb.column("overview_tab", overview)),
                                        ("Signals", sb.column("signals_tab", signal_children)),
                                        ("Evidence", sb.column("evidence_tab", evidence_children))],
                       a11y="Problem details"))
    return sb.messages(ids)


def problem_inbox_surface(problems: list[ProductProblem], source_health: dict[str, str]) -> list[dict[str, Any]]:
    """Problem Inbox (FR-C8, O1): SourceHealth (loud when a connector is down), problems table, open & filter.

    ``open_problem`` sends ``{problemIds: @/selection/problem_ids}``; ``filter`` sends
    ``{status: @/filter/status, classification: @/filter/classification}``.
    """
    sources = [{"source": name, "status": status, "healthy": _healthy(status)}
               for name, status in sorted(source_health.items())]
    unhealthy = [str(s["source"]) for s in sources if not s["healthy"]]
    rows: list[views.Row] = [{
        "problem_id": p.problem_id, "title": p.title.text, "classification": p.classification.text,
        "confidence": p.classification_confidence.value, "status": p.status.value, "signals": len(p.linked_signals),
        "customers": p.affected_customers.text if p.affected_customers else "",
        "open_questions": sum(1 for q in p.open_questions if q.status is QuestionStatus.OPEN),
        "partial_sources": ", ".join(p.sources_unavailable)} for p in problems]
    attention = sum(1 for p in problems if p.classification_confidence is Confidence.LOW or p.sources_unavailable
                    or any(q.status is QuestionStatus.OPEN for q in p.open_questions))
    sb = SurfaceBuilder("problem_inbox")
    sb.data.update({"source_health": sources, "table": views.table(_INBOX_COLUMNS, rows),
                    "selection": {"problem_ids": []}, "filter": {"status": [], "classification": []}})
    ids = [sb.text("title", heading("Problem inbox")),
           sb.text("summary", md(f"{len(problems)} problem(s) · {attention} need your attention"))]
    if unhealthy:
        ids.append(sb.text("partial_data", md("⚠ Partial data — unavailable: " + ", ".join(unhealthy)),
                           live="assertive"))
    ids.append(sb.slb("source_health", "SourceHealth", a11y="Source health", live="polite" if unhealthy else None,
                      sources=bind("source_health")))
    if problems:
        ids.append(sb.slb("inbox_table", "DataTable", a11y="Problems", columns=bind("table", "columns"),
                          rows=bind("table", "rows")))
        picker = sb.choice_picker("open_picker", "Problem", [(p.title.text, p.problem_id) for p in problems],
                                  bind("selection", "problem_ids"), a11y="Problem to open", multiple=False,
                                  filterable=True)
        opener = sb.button("open_btn", "Open problem", OPEN_PROBLEM, {"problemIds": bind("selection", "problem_ids")},
                           a11y="Open the selected problem", primary=True)
        ids.append(sb.row("open_row", [picker, opener]))
    else:
        ids.append(sb.text("empty", md("No problems match the current filter.")))
    ids.append(sb.row("filter_row", [
        sb.choice_picker("status_filter", "Status", [(label(s.value), s.value) for s in ProblemStatus],
                         bind("filter", "status"), a11y="Filter by status", multiple=True),
        sb.choice_picker("classification_filter", "Classification", [(label(c.value), c.value) for c in ProblemClass],
                         bind("filter", "classification"), a11y="Filter by classification", multiple=True),
        sb.button("filter_btn", "Apply filter", FILTER,
                  {"status": bind("filter", "status"), "classification": bind("filter", "classification")},
                  a11y="Apply the inbox filter")]))
    return sb.messages(ids)


def prototype_surface(spec: PrototypeSpec, report: VerificationReport | None, images: list[str], *,
                      action: ProposedAction | None = None,
                      min_reuse_ratio: float = DEFAULT_MIN_REUSE_RATIO) -> list[dict[str, Any]]:
    """Prototype Plan & Validation (FR-B1–B5): preview, DLS reuse, component mapping, justified new patterns.

    Pass the pending render/release *action* (with its *report*) to append the decision block.
    """
    if report is not None:
        _check_report_subject(report, spec)
    digest = None
    if action is not None:
        if report is None:
            raise ValueError("a decision block needs the verification report that backs the action")
        digest = _check_decision(action, spec, report)
    sections = views.prototype_sections(spec)
    ratio = spec.reuse_ratio
    sb = SurfaceBuilder(f"prototype_{spec.spec_id}")
    sb.data.update({"spec": {"spec_id": spec.spec_id, "brief_id": spec.brief_id,
                             "requirement_id": spec.requirement_id, "dls_library_file_key": spec.dls_library_file_key,
                             "dls_published_version": spec.dls_published_version},
                    "images": list(images), "sections": sections,
                    "reuse": {"reuse_ratio": round(ratio, 4), "reused_components": len(spec.component_keys()),
                              "new_patterns": len(spec.new_patterns), "min_reuse_ratio": min_reuse_ratio}})
    ids = [sb.text("title", heading("Prototype plan & validation")),
           sb.text("meta", md(f"{spec.spec_id} · brief {spec.brief_id} · requirement {spec.requirement_id} · DLS "
                              f"{spec.dls_library_file_key}@{spec.dls_published_version} · reuse {ratio:.0%} "
                              f"(target ≥ {min_reuse_ratio:.0%})"), caption=True)]
    if ratio < min_reuse_ratio:
        ids.append(sb.text("reuse_warning", md(f"⚠ DLS reuse {ratio:.0%} is below the {min_reuse_ratio:.0%} DLS "
                                               "reuse target — every new pattern must be justified (FR-B3)."),
                           live="polite"))
    if report is not None:
        ids += _verifier_block(sb, report)
    ids.append(sb.slb("preview", "PrototypePreview", a11y="Prototype screens", images=bind("images"),
                      reuse=bind("reuse")))
    ids += _mapping_block(sb, spec)
    if sections:
        ids.append(sb.text("patterns_title", heading("New patterns", 3)))
        ids += _section_blocks(sb, sections)
        for index, pattern in enumerate(spec.new_patterns):
            nearest = "; ".join(f"{c.get('component_key', '?')}: {c.get('why_insufficient', '')}"
                                for c in pattern.nearest_candidates) or "none recorded"
            ids.append(sb.text(f"pattern_{index}_nearest", md(f"{pattern.name} — nearest DLS candidates: {nearest}"),
                               caption=True))
    if action is not None and report is not None and digest is not None:
        ids += _decision_summary(sb, action, report, digest, diff=None, with_badge=False)
        ids += _decision_controls(sb, action, report, digest)
    return sb.messages(ids)


def knowledge_review_surface(article: KnowledgeArticleDraft, report: VerificationReport | None, *,
                             action: ProposedAction | None = None) -> list[dict[str, Any]]:
    """Knowledge Draft Review (FR-C4, AC6): symptom / root cause / resolution / workaround as cited ClaimLists.

    Pass the pending ``KB_PUBLISH_ARTICLE`` *action* (with its *report*) to append the decision block.
    """
    if report is not None:
        _check_report_subject(report, article)
    digest = None
    if action is not None:
        if report is None:
            raise ValueError("a decision block needs the verification report that backs the action")
        digest = _check_decision(action, article, report)
    sections = views.knowledge_sections(article)
    sb = SurfaceBuilder(f"kb_review_{article.article_id}_v{article.version}")
    sb.data.update({"article": {"article_id": article.article_id, "version": article.version,
                                "problem_id": article.problem_id, "applies_to_product": article.applies_to_product,
                                "applies_to_versions": list(article.applies_to_versions),
                                "audience": article.audience.value,
                                "derived_from_resolution": article.derived_from_resolution,
                                "approval_state": article.approval_state.value},
                    "sections": sections})
    ids = [sb.text("title", heading("Knowledge article review")),
           sb.text("meta", md(f"{article.article_id} · v{article.version} · {article.applies_to_product} "
                              f"{', '.join(article.applies_to_versions)} · audience {article.audience.value} · "
                              f"derived from {article.derived_from_resolution} · approval "
                              f"{article.approval_state.value}"), caption=True)]
    if report is None:
        ids.append(sb.text("unverified", md("Not verified yet — the Verifier has not checked this version."),
                           live="polite"))
    else:
        ids += _verifier_block(sb, report)
    ids += _section_blocks(sb, sections)
    if action is not None and report is not None and digest is not None:
        ids += _decision_summary(sb, action, report, digest, diff=None, with_badge=False)
        ids += _decision_controls(sb, action, report, digest)
    return sb.messages(ids)


def investigation_surface(pack: InvestigationPack) -> list[dict[str, Any]]:
    """Investigation Pack (FR-C1–C3, C5): timeline, related signals, prior resolutions, engagements, outcome.

    ``confirm_outcome`` sends ``{packId, outcome: @/outcome/selected}`` (the PM confirms — the agent only
    proposes); ``draft_kb`` / ``draft_bug`` send ``{packId}``.
    """
    sections = views.pack_sections(pack, include_timeline=False)
    related: list[views.Row] = [{"signal_id": link.signal_id, "link_type": link.link_type.value,
                                 "score": round(link.score, 4), "linked_by": link.linked_by} for link in pack.related]
    proposed = pack.outcome_label.value if pack.outcome_label else None
    sb = SurfaceBuilder(f"investigation_{pack.pack_id}")
    sb.data.update({"pack": {"pack_id": pack.pack_id, "trigger_signal_id": pack.trigger_signal_id,
                             "outcome_label": proposed},
                    "timeline": [{"at": None, "label": c.text, "kind": c.kind.value, "claim": c.model_dump(mode="json")}
                                 for c in pack.timeline],
                    "related": views.table(_RELATED_COLUMNS, related), "sections": sections,
                    "outcome": {"selected": [proposed] if proposed else []}})
    ids = [sb.text("title", heading("Investigation pack")),
           sb.text("meta", md(f"{pack.pack_id} · trigger {pack.trigger_signal_id} · {len(related)} related signal(s)"
                              f" · proposed outcome: {label(proposed) if proposed else 'none'} (you confirm)"),
                   caption=True),
           sb.slb("timeline", "Timeline", a11y="Investigation timeline", events=bind("timeline")),
           sb.text("related_title", heading("Related signals", 3)),
           sb.slb("related_table", "DataTable", a11y="Related signals", columns=bind("related", "columns"),
                  rows=bind("related", "rows"))]
    ids += _section_blocks(sb, sections)
    ids.append(sb.row("outcome_row", [
        sb.choice_picker("outcome_picker", "Outcome", [(label(o.value), o.value) for o in Outcome],
                         bind("outcome", "selected"), a11y="Investigation outcome", multiple=False),
        sb.button("confirm_outcome_btn", "Confirm outcome", CONFIRM_OUTCOME,
                  {"packId": pack.pack_id, "outcome": bind("outcome", "selected")},
                  a11y="Confirm the investigation outcome", primary=True)]))
    ids.append(sb.row("next_steps", [
        sb.button("draft_kb_btn", "Draft KB article", DRAFT_KB, {"packId": pack.pack_id},
                  a11y="Draft a knowledge article from this investigation"),
        sb.button("draft_bug_btn", "Draft bug", DRAFT_BUG, {"packId": pack.pack_id},
                  a11y="Draft a bug from this investigation")]))
    return sb.messages(ids)


def audit_surface(entries: list[LedgerEntry]) -> list[dict[str, Any]]:
    """Audit Ledger (GOV6, AC10): entries ordered by ``seq`` as a timeline and a table; filter and export.

    ``filter`` sends ``{entryTypes: @/filter/entry_types}``; ``export`` sends ``{fromSeq, toSeq}`` literals.
    """
    ordered = sorted(entries, key=lambda e: e.seq)
    timeline, rows = [], []
    for entry in ordered:
        dumped = entry.model_dump(mode="json")
        subjects = ", ".join(f"{s.type}:{s.id}" + (f"@v{s.version}" if s.version else "") for s in entry.subject_refs)
        actor = f"{entry.actor.type}:{entry.actor.id}"
        timeline.append({"at": dumped["recorded_utc"], "label": f"#{entry.seq} {entry.entry_type.value} · {actor}",
                         "kind": entry.entry_type.value, "detail": subjects})
        rows.append({"seq": entry.seq, "recorded_utc": dumped["recorded_utc"], "entry_type": entry.entry_type.value,
                     "actor": actor, "subjects": subjects, "entry_hash": entry.entry_hash,
                     "prev_hash": entry.prev_hash, "trace_id": entry.trace_id})
    first, last = (ordered[0].seq, ordered[-1].seq) if ordered else (0, 0)
    sb = SurfaceBuilder(f"audit_ledger_{first}_{last}" if ordered else "audit_ledger_empty")
    sb.data.update({"timeline": timeline, "table": views.table(_AUDIT_COLUMNS, rows), "filter": {"entry_types": []}})
    ids = [sb.text("title", heading("Audit ledger"))]
    if ordered:
        ids += [sb.text("summary", md(f"{len(ordered)} entries · seq {first}–{last} · head {ordered[-1].entry_hash}")),
                sb.slb("timeline", "Timeline", a11y="Ledger timeline", events=bind("timeline")),
                sb.slb("ledger_table", "DataTable", a11y="Ledger entries", columns=bind("table", "columns"),
                       rows=bind("table", "rows"))]
    else:
        ids.append(sb.text("empty", md("No ledger entries match the current filter.")))
    controls = [sb.choice_picker("entry_type_filter", "Entry types", [(t.value, t.value) for t in LedgerEntryType],
                                 bind("filter", "entry_types"), a11y="Filter by entry type", multiple=True),
                sb.button("filter_btn", "Apply filter", FILTER, {"entryTypes": bind("filter", "entry_types")},
                          a11y="Apply the ledger filter")]
    if ordered:
        controls.append(sb.button("export_btn", "Export", EXPORT, {"fromSeq": first, "toSeq": last},
                                  a11y=f"Export ledger entries {first} to {last}"))
    ids.append(sb.row("ledger_controls", controls))
    return sb.messages(ids)


def status_surface(surface_id: str, title: str, lines: list[str]) -> list[dict[str, Any]]:
    """Progress/status card (deterministic, basic catalog only). *surface_id* is sanitized like every id."""
    sb = SurfaceBuilder(surface_id)
    ids = [sb.text("status_title", heading(title))]
    ids += [sb.text(f"status_line_{i}", md(line), live="polite") for i, line in enumerate(lines)]
    return sb.messages(ids)


def delete_surface(surface_id: str) -> dict[str, Any]:
    """``deleteSurface`` for *surface_id* (sanitized exactly as the builders sanitize it)."""
    return {"version": A2UI_VERSION, "deleteSurface": {"surfaceId": sanitize_id(surface_id)}}
