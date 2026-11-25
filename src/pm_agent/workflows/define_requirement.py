"""W3 — Define requirement (docs/05 §4.3; FR-A5…FR-A10, AC2/AC3).

``load_problem → [readiness_assessor (LLM) → verify_readiness → question_generator (LLM) → verify_questions →
ask_pm ⏸ → record_pm_answers]* → [spec_writer + feasibility_assessor (LLM) → assemble_draft → render_and_verify
(V0–V10) → (duplicate decision ⏸)]* → propose → await_approval ⏸ → commit_if_approved``.

Loops are bounded (gather rounds, repair attempts). PM answers become ``pm_input`` evidence signals attributed to
the authenticated PM; the approval resume carries no authority — ``commit_if_approved`` re-reads the ledger.
"""

from __future__ import annotations

from typing import Any

from google.adk import Context
from google.adk.events import RequestInput
from google.adk.workflow import node
from pydantic import BaseModel

from pm_agent.a2ui import surfaces as ui
from pm_agent.agents.factory import LlmNodes
from pm_agent.agents.llm_schemas import FeasibilityOut, QuestionsOut, ReadinessOut, RequirementOut, to_claim_drafts
from pm_agent.connectors.base import make_signal
from pm_agent.grounding.chunking import chunk_signal
from pm_agent.grounding.verifier import ClaimCheckResult
from pm_agent.hitl.identity import IdentityError
from pm_agent.ports import Services
from pm_agent.schemas import (
    ActionType,
    Actor,
    Approver,
    ClaimKind,
    ClarifyingQuestion,
    DraftField,
    DuplicateCandidate,
    DuplicateDecision,
    FeasibilityAssessment,
    FeasibilityVerdict,
    FieldStatus,
    LedgerEntryType,
    LinkType,
    PersonRef,
    ProblemStatus,
    ReadinessAssessment,
    ReadinessItem,
    ReadinessItemName,
    ReadinessStatus,
    RecordType,
    RequirementDraft,
    SourceSystem,
    SubjectRef,
    sha256_hex,
)
from pm_agent.workflows.common import dump_blocked, problem_pack, resolve_problem, save_draft
from pm_agent.workflows.approval import build_approval_nodes, report_payload
from pm_agent.workflows.rendering import render_ado_payload

TEMPLATE = "slb-ado-requirement-v1"
REQUIRED_FIELDS = ("summary", "acceptance_criteria")
MAX_GATHER_ROUNDS = 3
MAX_DRAFT_ATTEMPTS = 3
ADO = ActionType.ADO_CREATE_WORK_ITEM.value

PM_ANSWERS_SCHEMA: dict[str, Any] = {"type": "object", "properties": {
    "answers": {"type": "array", "items": {"type": "object", "properties": {
        "question_id": {"type": "string"}, "checklist_item": {"type": "string"}, "text": {"type": "string"}}}},
    "proceed_with_gaps": {"type": "boolean"},
    "assertion": {"type": "string", "description": "IAP assertion of the answering PM (added by the workbench)"}}}
DUPLICATE_SCHEMA: dict[str, Any] = {"type": "object", "properties": {
    "decisions": {"type": "array", "items": {"type": "object", "properties": {
        "ref": {"type": "string"}, "decision": {"type": "string", "enum": ["linked", "merged", "distinct"]}}}},
    "reason": {"type": "string"}, "assertion": {"type": "string"}}}


def default_question(item: str) -> str:
    return f"Please provide the {item.replace('_', ' ')} for this problem."


def _draft_field(checked: ClaimCheckResult) -> DraftField:
    """Verified facts populate a field (PARTIAL if some claims were blocked); inference alone marks it INFERRED."""
    facts = [c for c in checked.passed if c.kind is not ClaimKind.INFERENCE]
    inferred = [c for c in checked.passed if c.kind is ClaimKind.INFERENCE]
    if facts:
        return DraftField(status=FieldStatus.PARTIAL if checked.blocked else FieldStatus.POPULATED, claims=facts)
    if inferred:
        return DraftField(status=FieldStatus.INFERRED, claims=inferred)
    return DraftField(status=FieldStatus.MISSING)


def build_define_requirement(services: Services, llm: LlmNodes) -> Any:  # pylint: disable=too-many-locals,too-many-statements
    """Return the ``define_requirement`` dynamic node (input: ``{"problem_id"}`` or text naming ``prb_…``)."""
    settings, verifier = services.settings, services.verifier
    template = settings.templates[TEMPLATE]
    checklist = settings.readiness_checklist

    def _approver(assertion: Any) -> Approver | None:
        if not isinstance(assertion, str) or not assertion:
            return None
        try:
            return services.identity.verify(assertion)
        except IdentityError:
            return None

    def _set_problem(problem_id: str, **changes: Any) -> None:
        services.problems.save(services.problems.get(problem_id).model_copy(update=changes))

    def load_problem(node_input: Any) -> dict[str, Any]:
        problem = resolve_problem(services, node_input, ask="Which product problem? Mention its id (prb_...).")
        if isinstance(problem, dict):
            return problem
        return {"problem_id": problem.problem_id, "pack": problem_pack(services, problem.problem_id)}

    def _readiness_item(name: str, out: Any) -> tuple[ReadinessItem, ClaimCheckResult]:
        """One checklist item: PRESENT only with verified claims and nothing blocked; gaps never carry atoms."""
        checked = verifier.check_claims(to_claim_drafts(out.claims)) if out else ClaimCheckResult()
        if out and out.status == "present" and checked.passed and not checked.blocked:
            status = ReadinessStatus.PRESENT
        else:
            status = ReadinessStatus.PARTIAL if checked.passed else ReadinessStatus.MISSING
        gap_text = out.gap if out and out.gap and not services.atoms.has_atoms(out.gap) else None
        gap = None if status is ReadinessStatus.PRESENT else (
            gap_text or f"No verified evidence yet for {name.replace('_', ' ')}.")
        return ReadinessItem(item=ReadinessItemName(name), status=status, claims=checked.passed, gap=gap), checked

    def verify_readiness(node_input: dict[str, Any]) -> dict[str, Any]:
        raw = ReadinessOut.model_validate(node_input["raw"])
        by_item = {str(item.item): item for item in raw.items}
        items, blocked = [], []
        for name in checklist.items:
            item, checked = _readiness_item(name, by_item.get(name))
            items.append(item)
            blocked += checked.blocked
        assessment = ReadinessAssessment(checklist_id=checklist.id, items=items)
        gaps = [i.item.value for i in assessment.gaps]
        _set_problem(node_input["problem_id"], readiness=assessment,
                     status=ProblemStatus.GATHERING if gaps else ProblemStatus.READY_FOR_DRAFT)
        return {"readiness": assessment.model_dump(mode="json"), "gaps": gaps, "blocked": dump_blocked(blocked)}

    def verify_questions(node_input: dict[str, Any]) -> dict[str, Any]:
        raw = QuestionsOut.model_validate(node_input["raw"])
        questions = []
        for gap in node_input["gaps"]:
            proposed = next((q for q in raw.questions if q.checklist_item == gap), None)
            why = verifier.check_claims(to_claim_drafts(proposed.why)).passed if proposed else []
            quotes = [c.quote for claim in why for c in claim.citations]
            grounded = proposed is not None and not services.atoms.unsupported(proposed.text, quotes)
            questions.append(ClarifyingQuestion(checklist_item=ReadinessItemName(gap), why_asked=why,
                                                text=proposed.text if grounded and proposed else
                                                default_question(gap)))
        problem_id = node_input["problem_id"]
        _set_problem(problem_id, open_questions=questions)
        readiness = ReadinessAssessment.model_validate(node_input["readiness"])
        return {"problem_id": problem_id, "questions": [q.model_dump(mode="json") for q in questions],
                "a2ui": ui.readiness_surface(problem_id, readiness, questions)}

    async def ask_pm(node_input: dict[str, Any]) -> Any:
        yield RequestInput(message="Clarifying questions need your answers before a requirement can be drafted.",
                           payload=node_input, response_schema=PM_ANSWERS_SCHEMA)

    def record_pm_answers(node_input: dict[str, Any]) -> dict[str, Any]:
        """Each answer → ledger ``PM_INPUT_RECORDED`` → a ``pm_input`` signal citable as ``pm_judgment``."""
        problem_id, reply = node_input["problem_id"], node_input.get("reply") or {}
        approver = _approver(reply.get("assertion"))
        if approver is None:
            return {"recorded": [], "error": "unauthenticated", "pack": problem_pack(services, problem_id)}
        recorded = []
        for answer in reply.get("answers") or []:
            text = str(answer.get("text") or "").strip()
            if not text:
                continue
            entry = services.ledger.record(
                LedgerEntryType.PM_INPUT_RECORDED, actor=Actor(type="human", id=approver.sub),
                subject_refs=[SubjectRef(type="product_problem", id=problem_id)],
                payload={"problem_id": problem_id, "question_id": answer.get("question_id"),
                         "checklist_item": answer.get("checklist_item"),
                         "answer_sha256": "sha256:" + sha256_hex(text)})
            signal = make_signal(redactor=services.redactor, source_system=SourceSystem.PM_INPUT,
                                 source_record_id=entry.entry_id, source_version="1",
                                 record_type=RecordType.PM_ANSWER, permalink=f"pm-agent://ledger/{entry.entry_id}",
                                 raw_text=text, captured_utc=services.clock(), connector="pm-workbench",
                                 author=PersonRef(token="PERSON_" + sha256_hex(approver.sub)[:6].upper(),
                                                  role_hint="pm"))
            services.evidence.add_signal(signal)
            for chunk in chunk_signal(signal):
                services.evidence.add_chunk(chunk)
            services.problems.link(problem_id, signal.signal_id, LinkType.PM_CONFIRMED, 1.0, approver.sub)
            recorded.append(signal.signal_id)
        return {"recorded": recorded, "pack": problem_pack(services, problem_id)}

    def assemble_draft(node_input: dict[str, Any]) -> dict[str, Any]:
        """Verified claims only: blocked claims are removed, never repaired; empty fields become ``missing``."""
        raw = RequirementOut.model_validate(node_input["raw_fields"])
        by_name = {f.name: f for f in raw.fields if f.name in template.fields}
        fields, blocked = {}, []
        for name in template.fields:
            out = by_name.get(name)
            checked = verifier.check_claims(to_claim_drafts(out.claims)) if out else ClaimCheckResult()
            blocked += checked.blocked
            fields[name] = _draft_field(checked)
        feasibility_raw = FeasibilityOut.model_validate(node_input["raw_feasibility"])
        feasibility_claims = verifier.check_claims(to_claim_drafts(feasibility_raw.claims))
        components = verifier.check_claims(to_claim_drafts(feasibility_raw.affected_components))
        blocked += feasibility_claims.blocked + components.blocked
        draft = RequirementDraft(problem_id=node_input["problem_id"], version=node_input["attempt"],
                                 template=TEMPLATE, work_item_type=template.work_item_type, fields=fields,
                                 feasibility=FeasibilityAssessment(verdict=FeasibilityVerdict(feasibility_raw.verdict),
                                                                   claims=feasibility_claims.passed,
                                                                   affected_components=components.passed))
        ref = save_draft(services, "requirement_draft", draft,
                         related=[SubjectRef(type="product_problem", id=draft.problem_id)],
                         payload={"blocked_claims": len(blocked)})
        missing = [n for n, f in fields.items() if f.status is FieldStatus.MISSING]
        return {"draft_ref": ref, "blocked": dump_blocked(blocked), "missing": missing,
                "missing_required": [n for n in REQUIRED_FIELDS if n in missing]}

    def render_and_verify(node_input: dict[str, Any]) -> dict[str, Any]:
        _, subject = services.subjects.get(node_input["draft_ref"])
        assert isinstance(subject, RequirementDraft)
        payload = render_ado_payload(subject, template)
        report = verifier.verify_subject(subject, subject_type="requirement_draft", commit_bound=True,
                                         target_action=ADO, target_payload=payload)
        result: dict[str, Any] = {"draft_ref": node_input["draft_ref"], **report_payload(services, report)}
        if report.overall.value == "NEEDS_PM_DECISION":
            text = " ".join(c.text for c in subject.all_claims())
            threshold = settings.verifier.near_duplicate_threshold
            candidates = [c for c in services.near_duplicates.find(text) if c.similarity >= threshold]
            result["candidates"] = [c.model_dump(mode="json") for c in candidates]
            result["a2ui"] = ui.duplicate_decision_surface(subject.requirement_id, candidates)
        return result

    async def ask_duplicate_decision(node_input: dict[str, Any]) -> Any:
        yield RequestInput(message="Possible duplicates found: link, merge or proceed?", payload=node_input,
                           response_schema=DUPLICATE_SCHEMA)

    def apply_duplicate_decision(node_input: dict[str, Any]) -> dict[str, Any]:
        reply = node_input.get("reply") or {}
        approver = _approver(reply.get("assertion"))
        if approver is None:
            return {"status": "unauthenticated"}
        decided = {d.get("ref"): d.get("decision") for d in reply.get("decisions") or []}
        candidates = [DuplicateCandidate.model_validate(c).model_copy(
            update={"decision": DuplicateDecision(decided.get(c["ref"], "pending"))})
            for c in node_input["candidates"]]
        services.ledger.record(LedgerEntryType.PM_INPUT_RECORDED, actor=Actor(type="human", id=approver.sub),
                               payload={"draft_ref": node_input["draft_ref"],
                                        "duplicate_decisions": {c.ref: c.decision.value for c in candidates},
                                        "reason_sha256": "sha256:" + sha256_hex(str(reply.get("reason", "")))})
        linked = [c.ref for c in candidates if c.decision in (DuplicateDecision.LINKED, DuplicateDecision.MERGED)]
        if linked:
            return {"status": "linked_to_existing", "refs": linked}
        _, draft = services.subjects.get(node_input["draft_ref"])
        assert isinstance(draft, RequirementDraft)
        updated = draft.model_copy(update={"version": draft.version + 1, "near_duplicates": candidates})
        return {"status": "proceed", "draft_ref": services.subjects.put("requirement_draft", updated)}

    def _target(subject: BaseModel) -> dict[str, Any]:
        assert isinstance(subject, RequirementDraft)
        ado = settings.connectors.get("ado", {})
        return {"system": "ado", "organization": ado.get("organization"), "project": ado.get("project"),
                "work_item_type": subject.work_item_type}

    def _render(subject: BaseModel) -> dict[str, Any]:
        assert isinstance(subject, RequirementDraft)
        return render_ado_payload(subject, template)

    def _on_status(subject: BaseModel, status: str) -> None:
        assert isinstance(subject, RequirementDraft)
        mapped = {"pending_approval": ProblemStatus.PENDING_APPROVAL, "committed": ProblemStatus.COMMITTED,
                  "rejected": ProblemStatus.GATHERING}.get(status)
        if mapped is not None:
            _set_problem(subject.problem_id, status=mapped)

    tail = build_approval_nodes(
        services, prefix="requirement", action_type=ADO, subject_type="requirement_draft", render=_render,
        target=_target, on_status=_on_status, message="Approval required: create the ADO work item exactly as shown.",
        surfaces=lambda action, subject, report: ui.requirement_review_surface(subject, report)  # type: ignore[arg-type]
        + ui.approval_surface(action, subject, report))

    @node(rerun_on_resume=True, name="define_requirement")
    async def define_requirement(ctx: Context, node_input: Any) -> dict[str, Any]:
        loaded = await ctx.run_node(load_problem, node_input)
        if loaded.get("error"):
            return {"status": loaded["error"], "message": loaded["message"]}
        problem_id, pack = loaded["problem_id"], loaded["pack"]
        for _ in range(MAX_GATHER_ROUNDS):
            raw = await ctx.run_node(llm["readiness_assessor"], {"task": "readiness", "checklist": checklist.items,
                                                                  **pack})
            readiness = await ctx.run_node(verify_readiness, {"problem_id": problem_id, "raw": raw})
            if not readiness["gaps"]:
                break
            raw_q = await ctx.run_node(llm["question_generator"], {"task": "questions", "gaps": readiness["gaps"],
                                                                    **pack})
            questions = await ctx.run_node(verify_questions, {"problem_id": problem_id, "gaps": readiness["gaps"],
                                                              "readiness": readiness["readiness"], "raw": raw_q})
            reply = await ctx.run_node(ask_pm, questions)
            if (reply or {}).get("proceed_with_gaps"):
                break
            recorded = await ctx.run_node(record_pm_answers, {"problem_id": problem_id, "reply": reply})
            pack = recorded["pack"]
        feedback: list[str] = []
        verified: dict[str, Any] = {}
        for attempt in range(1, MAX_DRAFT_ATTEMPTS + 1):
            raw_fields = await ctx.run_node(llm["spec_writer"], {"task": "draft", "template": TEMPLATE,
                                                                  "template_fields": template.fields,
                                                                  "previous_attempt_problems": feedback, **pack})
            raw_feasibility = await ctx.run_node(llm["feasibility_assessor"], {"task": "feasibility", **pack})
            draft = await ctx.run_node(assemble_draft, {"problem_id": problem_id, "attempt": attempt,
                                                        "raw_fields": raw_fields, "raw_feasibility": raw_feasibility})
            if draft["missing_required"]:
                feedback = [f"required field {name} has no verified claims" for name in draft["missing_required"]]
                continue
            verified = await ctx.run_node(render_and_verify, {"draft_ref": draft["draft_ref"]})
            if verified["overall"] == "NEEDS_PM_DECISION":
                reply = await ctx.run_node(ask_duplicate_decision, verified)
                decision = await ctx.run_node(apply_duplicate_decision, {**verified, "reply": reply})
                if decision["status"] != "proceed":
                    return {"status": decision["status"], "problem_id": problem_id, **decision}
                verified = await ctx.run_node(render_and_verify, {"draft_ref": decision["draft_ref"]})
            if verified["overall"] == "PASS":
                break
            feedback = verified["failed"]
        else:
            return {"status": "needs_input", "problem_id": problem_id, "problems": feedback,
                    "draft_ref": verified.get("draft_ref")}
        outcome = await tail.run(ctx, {"subject_ref": verified["draft_ref"], "report": verified["report"]})
        return {**outcome, "problem_id": problem_id, "draft_ref": verified["draft_ref"]}

    return define_requirement
