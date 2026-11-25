"""Synthetic A2UI test kit — deterministic entities (no SLB data) and A2UI message inspection helpers.

Shared by ``tests/unit/test_a2ui_surfaces.py`` and ``tests/contract/test_a2ui.py``. Every value here is invented
(WITSML-style channel names, ``CNE`` = the synthetic "Contoso Energy" gazetteer entry, pseudonymous person tokens).
"""
from __future__ import annotations

import json
import re
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from pm_agent.schemas.subjects import subject_digest, subject_id
from pm_agent.hitl.ledger import GENESIS_HASH
from pm_agent.schemas import (
    ActionType,
    Actor,
    CheckResult,
    CheckStatus,
    Citation,
    Claim,
    ClaimKind,
    ClarifyingQuestion,
    Confidence,
    Customer,
    DraftField,
    DuplicateCandidate,
    DuplicateDecision,
    FieldStatus,
    InvestigationPack,
    KnowledgeArticleDraft,
    LedgerEntry,
    LedgerEntryType,
    LinkType,
    NewPattern,
    Outcome,
    PersonRef,
    ProblemClass,
    ProblemStatus,
    ProductProblem,
    ProposedAction,
    PrototypeScreen,
    PrototypeSpec,
    QuestionStatus,
    ReadinessAssessment,
    ReadinessItem,
    ReadinessItemName,
    ReadinessStatus,
    RecordType,
    RequirementDraft,
    Signal,
    SignalLink,
    SourceSystem,
    Span,
    SpecNode,
    SubjectRef,
    VerificationOutcome,
    VerificationReport,
    content_sha256,
)
from pm_agent.schemas.problems import SuggestedRespondent
from tests.support import AGENT_ID, PD_QUOTE, PD_TEXT, Hitl, fact, make_hitl, prototype_spec

FIXTURES = Path(__file__).resolve().parent
T0 = datetime(2026, 11, 20, 9, 30, tzinfo=UTC)

PROBLEM_ID = "prb_01JD3XAMPLE00000000000001"
PROBLEM_2_ID = "prb_01JD3XAMPLE00000000000002"
REQUIREMENT_ID = "draft-req_01JD3XAMPLE000000000001"
ARTICLE_ID = "draft-kb_01JD3XAMPLE0000000000001"
PACK_ID = "inv_01JD3XAMPLE00000000000001"
SPEC_ID = "spec_01JD3XAMPLE0000000000001"
SIG_PD = "sig_pd_1"
SIG_SNOW = "sig_snow_1"
IMAGES = ["https://figma.example/render/spec_1/approval.png", "https://figma.example/render/spec_1/evidence.png"]
TEMPLATE_FIELDS = ["summary", "user_workflow", "desired_outcome", "in_scope", "out_of_scope", "acceptance_criteria",
                   "edge_cases", "dependencies", "technical_constraints"]
SOURCE_HEALTH = {"ado": "ok", "servicenow": "unavailable", "pagerduty": "ok"}

SNOW_TEXT = ("Case CS0000001: after the mapping upload the TVDHD channel is still missing from TIME_SURFACE; "
             "customer CNE asked for an alert when standardized logs are incomplete.")
SNOW_QUOTE = "the TVDHD channel is still missing from TIME_SURFACE"
LATE_UPLOAD = "the standardization mapping file was uploaded after the channel streamed"

_PERMALINKS = {SourceSystem.PAGERDUTY: "https://example.pagerduty.com/incidents/",
               SourceSystem.SERVICENOW: "https://example.service-now.com/case/"}


# ------------------------------------------------------------------------------------------------ entities
def cite(quote: str = PD_QUOTE, *, snow: bool = False) -> Citation:
    """A resolved citation whose span really locates *quote* in the synthetic source text."""
    text, system, record, evidence, signal = ((SNOW_TEXT, SourceSystem.SERVICENOW, "CS0000001", "ev_snow_1", SIG_SNOW)
                                              if snow else
                                              (PD_TEXT, SourceSystem.PAGERDUTY, "90000001", "ev_pd_1", SIG_PD))
    start = text.index(quote)
    return Citation(evidence_id=evidence, signal_id=signal, source_system=system, source_record_id=record,
                    permalink=_PERMALINKS[system] + record, span=Span(start=start, end=start + len(quote)),
                    quote=quote)


def claim(claim_id: str, text: str, kind: ClaimKind = ClaimKind.FACT, *citations: Citation) -> Claim:
    return Claim(claim_id=claim_id, text=text, kind=kind, citations=list(citations))


def requirement_draft() -> RequirementDraft:
    """Populated (2 citations), partial, inferred and missing fields — every state the review surface must show."""
    return RequirementDraft(
        requirement_id=REQUIREMENT_ID, version=2, problem_id=PROBLEM_ID, work_item_type="Requirement",
        fields={
            "summary": DraftField(status=FieldStatus.POPULATED, claims=[
                claim("clm_sum_1", "TVDHD was not populated into TIME_SURFACE.", ClaimKind.FACT,
                      cite(), cite(SNOW_QUOTE, snow=True))]),
            "user_workflow": DraftField(status=FieldStatus.PARTIAL, claims=[
                claim("clm_wf_1", "The mapping file was uploaded after the channel streamed.", ClaimKind.FACT,
                      cite(LATE_UPLOAD))]),
            "acceptance_criteria": DraftField(status=FieldStatus.INFERRED, claims=[
                claim("clm_ac_1", "Late mapping uploads should backfill TIME_SURFACE.", ClaimKind.INFERENCE)]),
            "edge_cases": DraftField(status=FieldStatus.MISSING),
        },
        near_duplicates=[DuplicateCandidate(ref="1234567", system="ado", similarity=0.91, method="semantic")])


def report_for(subject: BaseModel, subject_type: str, *, overall: VerificationOutcome = VerificationOutcome.PASS,
               blocked: tuple[str, ...] = (), report_id: str = "vr_01JD3XAMPLE00000000000001") -> VerificationReport:
    """An (unsigned, display-only) report that binds *subject* exactly as the Verifier would."""
    checks = [CheckResult(check_id=f"V{i}", status=CheckStatus.PASS) for i in range(11)]
    if overall is not VerificationOutcome.PASS:
        checks[3] = CheckResult(check_id="V3", status=CheckStatus.FAIL, details="quote not found in evidence",
                                claim_ids=list(blocked))
    return VerificationReport(
        report_id=report_id,
        subject_ref=SubjectRef(type=subject_type, id=subject_id(subject), version=getattr(subject, "version", None),
                               sha256=subject_digest(subject)),
        subject_sha256=subject_digest(subject), verifier_version="verifier-0.1.0",
        policy_versions={"policy": "policy-1"}, checks=checks, overall=overall, blocked_claim_ids=list(blocked),
        created_utc=T0)


def signals() -> list[Signal]:
    return [
        Signal(signal_id=SIG_PD, source_system=SourceSystem.PAGERDUTY, source_record_id="90000001",
               source_version="1", record_type=RecordType.INCIDENT,
               permalink=_PERMALINKS[SourceSystem.PAGERDUTY] + "90000001", captured_utc=T0,
               author=PersonRef(token="PERSON_9F2C1A", role_hint="RTDS SRE"),
               customer=Customer(name="CNE", region="NAM"), product="DrillOps", title="TVDHD not populated",
               raw_text=PD_TEXT),
        Signal(signal_id=SIG_SNOW, source_system=SourceSystem.SERVICENOW, source_record_id="CS0000001",
               source_version="4", record_type=RecordType.CASE,
               permalink=_PERMALINKS[SourceSystem.SERVICENOW] + "CS0000001", captured_utc=T0 + timedelta(days=2),
               author=PersonRef(token="PERSON_77AB01", role_hint="L2 Support"),
               customer=Customer(name="CNE", region="NAM"), product="DrillOps", raw_text=SNOW_TEXT),
    ]


def product_problem() -> ProductProblem:
    return ProductProblem(
        problem_id=PROBLEM_ID, version=3, product_line="DrillOps",
        title=claim("clm_title", "TVDHD channel missing from TIME_SURFACE", ClaimKind.FACT, cite()),
        classification=claim("clm_class", "defect", ClaimKind.CLASSIFICATION, cite(SNOW_QUOTE, snow=True)),
        classification_confidence=Confidence.LOW,
        linked_signals=[SignalLink(signal_id=SIG_PD, link_type=LinkType.HARD_REF, score=1.0, linked_by="correlator"),
                        SignalLink(signal_id=SIG_SNOW, link_type=LinkType.SEMANTIC, score=0.91, linked_by="correlator")],
        affected_customers=claim("clm_cust", "1 affected customer: CNE.", ClaimKind.AGGREGATE, cite("Customer: CNE")),
        request_frequency=claim("clm_freq", "2 requesting signals in the last 90 days.", ClaimKind.AGGREGATE,
                                cite(), cite(SNOW_QUOTE, snow=True)),
        impact_business=[claim("clm_imp_b", "Standardized logs are incomplete for the customer.", ClaimKind.FACT,
                               cite(PD_QUOTE))],
        impact_customer=[claim("clm_imp_c", "The customer asked for an alert on incomplete standardized logs.",
                               ClaimKind.FACT,
                               cite("customer CNE asked for an alert when standardized logs are incomplete",
                                    snow=True))],
        duplicates=[DuplicateCandidate(ref="1234567", system="ado", similarity=0.91, method="semantic")],
        sources_unavailable=["servicenow"], owner_pm=PersonRef(token="PERSON_PM0001"),
        status=ProblemStatus.GATHERING)


def second_problem() -> ProductProblem:
    return ProductProblem(
        problem_id=PROBLEM_2_ID, title=claim("clm_title_2", "Late mapping uploads are not backfilled", ClaimKind.FACT,
                                             cite(LATE_UPLOAD)),
        classification=claim("clm_class_2", ProblemClass.ENHANCEMENT.value, ClaimKind.CLASSIFICATION,
                             cite(LATE_UPLOAD)),
        classification_confidence=Confidence.HIGH, status=ProblemStatus.READY_FOR_DRAFT)


def readiness() -> ReadinessAssessment:
    return ReadinessAssessment(items=[
        ReadinessItem(item=ReadinessItemName.WHO_AFFECTED, status=ReadinessStatus.PRESENT,
                      claims=[claim("clm_r_who", "CNE operations are affected.", ClaimKind.FACT, cite("Customer: CNE"))]),
        ReadinessItem(item=ReadinessItemName.CURRENT_VS_DESIRED_WORKFLOW, status=ReadinessStatus.PARTIAL,
                      claims=[claim("clm_r_wf", "Mapping uploads can arrive after streaming starts.", ClaimKind.FACT,
                                    cite(LATE_UPLOAD))],
                      gap="The desired workflow is not described.", question_ids=["q_wf"]),
        ReadinessItem(item=ReadinessItemName.SHOULD_DO_SHOULD_NOT, status=ReadinessStatus.PRESENT,
                      claims=[claim("clm_r_sd", "TVDHD must reach TIME_SURFACE.", ClaimKind.FACT, cite())]),
        ReadinessItem(item=ReadinessItemName.ACCEPTANCE_CRITERIA, status=ReadinessStatus.MISSING,
                      gap="No acceptance criteria were found.", question_ids=["q_ac"]),
        ReadinessItem(item=ReadinessItemName.EDGE_CASES, status=ReadinessStatus.MISSING,
                      gap="No edge cases were found.", question_ids=["q_edge"]),
        ReadinessItem(item=ReadinessItemName.DEPENDENCIES_CONSTRAINTS, status=ReadinessStatus.PRESENT,
                      claims=[claim("clm_r_dep", "Standardization depends on the mapping file.", ClaimKind.FACT,
                                    cite(LATE_UPLOAD))]),
    ])


QUESTION_WITH_MARKDOWN = "Which edge cases matter (e.g. #1 *late* uploads, [backfill] or `restream`)?"


def questions() -> list[ClarifyingQuestion]:
    respondent = PersonRef(token="PERSON_9F2C1A", role_hint="RTDS SRE")
    return [
        ClarifyingQuestion(
            question_id="q_ac", checklist_item=ReadinessItemName.ACCEPTANCE_CRITERIA,
            text="When does TVDHD count as populated in TIME_SURFACE?",
            why_asked=[claim("clm_q_ac", "TVDHD was not populated into TIME_SURFACE.", ClaimKind.FACT, cite())],
            suggested_respondents=[SuggestedRespondent(
                person=respondent,
                basis=claim("clm_q_ac_basis", "The root cause note names the late mapping upload.", ClaimKind.FACT,
                            cite(LATE_UPLOAD)))]),
        ClarifyingQuestion(question_id="q_edge", checklist_item=ReadinessItemName.EDGE_CASES,
                           text=QUESTION_WITH_MARKDOWN),
        ClarifyingQuestion(question_id="q_wf", checklist_item=ReadinessItemName.CURRENT_VS_DESIRED_WORKFLOW,
                           text="What is the desired workflow?", status=QuestionStatus.ANSWERED,
                           answer_signal_id="sig_pm_1"),
    ]


def duplicate_candidates() -> list[DuplicateCandidate]:
    return [DuplicateCandidate(ref="1234567", system="ado", similarity=0.91, method="semantic"),
            DuplicateCandidate(ref="CS0000001", system="servicenow", similarity=0.84, method="minhash"),
            DuplicateCandidate(ref="2830100", system="ado", similarity=0.70, method="semantic",
                               decision=DuplicateDecision.DISTINCT)]


def knowledge_article() -> KnowledgeArticleDraft:
    return KnowledgeArticleDraft(
        article_id=ARTICLE_ID, problem_id=PROBLEM_ID,
        title=claim("clm_kb_title", "TVDHD missing from TIME_SURFACE after a late mapping upload", ClaimKind.FACT,
                    cite()),
        symptom=[claim("clm_kb_sym", "TVDHD is not populated into TIME_SURFACE.", ClaimKind.FACT, cite())],
        root_cause=[claim("clm_kb_rc", "The mapping file was uploaded after the channel streamed.", ClaimKind.FACT,
                          cite(LATE_UPLOAD))],
        resolution=[claim("clm_kb_res", "Re-upload the mapping file, then restream the channel.",
                          ClaimKind.INFERENCE)],
        applies_to_product="DrillOps", applies_to_versions=["2026.2"], derived_from_resolution=PACK_ID)


def investigation_pack() -> InvestigationPack:
    return InvestigationPack(
        pack_id=PACK_ID, trigger_signal_id=SIG_PD,
        related=[SignalLink(signal_id=SIG_SNOW, link_type=LinkType.SEMANTIC, score=0.91, linked_by="correlator")],
        prior_resolutions=[claim("clm_prior", "A similar case was resolved by re-uploading the mapping file.",
                                 ClaimKind.FACT, cite(SNOW_QUOTE, snow=True))],
        timeline=[claim("clm_tl_1", "The channel streamed before the mapping upload.", ClaimKind.FACT,
                        cite(LATE_UPLOAD)),
                  claim("clm_tl_2", "The customer reported the missing channel.", ClaimKind.FACT,
                        cite(SNOW_QUOTE, snow=True))],
        suggested_engagements=[claim("clm_eng", "Engage RTDS SRE, who authored the root-cause note.", ClaimKind.FACT,
                                     cite(LATE_UPLOAD))],
        outcome=claim("clm_outcome", Outcome.WORKAROUND.value, ClaimKind.CLASSIFICATION, cite(LATE_UPLOAD)),
        outcome_label=Outcome.WORKAROUND)


def _hash(n: int) -> str:
    return "sha256:" + f"{n:064x}"


def ledger_entries() -> list[LedgerEntry]:
    """Three chained entries, deliberately out of order (the audit surface must sort by ``seq``)."""
    action_id = "act_01JD3XAMPLE00000000000001"
    return [
        LedgerEntry(seq=2, entry_id="le_2", entry_type=LedgerEntryType.DECISION_RECORDED,
                    actor=Actor(type="human", id="pm-1@slb.test"),
                    payload={"action_id": action_id, "decision": "approve"}, prev_hash=_hash(1), entry_hash=_hash(2),
                    recorded_utc=T0 + timedelta(minutes=5), trace_id="trace-1"),
        LedgerEntry(seq=1, entry_id="le_1", entry_type=LedgerEntryType.ACTION_PROPOSED,
                    subject_refs=[SubjectRef(type="requirement_draft", id=REQUIREMENT_ID, version=2)],
                    actor=Actor(type="agent", id=AGENT_ID), payload={"action_id": action_id},
                    prev_hash=GENESIS_HASH, entry_hash=_hash(1), recorded_utc=T0, trace_id="trace-1"),
        LedgerEntry(seq=3, entry_id="le_3", entry_type=LedgerEntryType.TOKEN_ISSUED,
                    actor=Actor(type="system", id="mcp-ledger"), payload={"action_id": action_id, "jti": "tok_1"},
                    prev_hash=_hash(2), entry_hash=_hash(3), recorded_utc=T0 + timedelta(minutes=6)),
    ]


def display_prototype_spec() -> PrototypeSpec:
    """One DLS instance + one justified new pattern ⇒ reuse ratio 0.5 (below the 0.9 policy target)."""
    screen = PrototypeScreen(name="Approval", width=1440, height=900, nodes=[
        SpecNode(kind="frame", name="Body", children=[
            SpecNode(kind="instance", name="Approve", component_key="dls:button/primary", variant_props={"size": "md"}),
            SpecNode(kind="text", name="Heading", text_claim_id="clm_sum_1")])])
    pattern = NewPattern(name="Evidence timeline strip",
                         justification=[claim("clm_np_1", "Signals must be shown on a time axis.", ClaimKind.FACT,
                                              cite(LATE_UPLOAD))],
                         nearest_candidates=[{"component_key": "dls:table/default",
                                              "why_insufficient": "no time axis"}])
    return PrototypeSpec(spec_id=SPEC_ID, brief_id="brief_1", requirement_id=REQUIREMENT_ID,
                         dls_library_file_key="lib", dls_published_version="v1", screens=[screen],
                         new_patterns=[pattern])


# ----------------------------------------------------------------------------------- real HITL proposals
@dataclass
class Proposal:
    """A real proposal from the in-process HITL chain: signed, commit-bound PASS report for the exact payload."""

    hitl: Hitl
    action: ProposedAction
    subject: BaseModel
    report: VerificationReport


def _proposal(hitl: Hitl, action: ProposedAction) -> Proposal:
    return Proposal(hitl=hitl, action=action, subject=hitl.subjects.get(action.draft_ref)[1],
                    report=hitl.approvals.get_report(action.verification_report_id))


def ado_proposal() -> Proposal:
    hitl = make_hitl()
    return _proposal(hitl, hitl.propose_ado())


def figma_proposal() -> Proposal:
    hitl = make_hitl()
    spec = prototype_spec()
    action = hitl.propose(spec, "prototype_spec", ActionType.FIGMA_RENDER_PROTOTYPE.value,
                          {"spec_sha256": content_sha256(spec), "reuse_ratio": 1.0})
    return _proposal(hitl, action)


def kb_proposal() -> Proposal:
    hitl = make_hitl()
    passed = [hitl.verifier.check_claims([fact()]).passed[0] for _ in range(4)]
    article = KnowledgeArticleDraft(problem_id="prb_1", title=passed[0], symptom=[passed[1]],
                                    root_cause=[passed[2]], resolution=[passed[3]], applies_to_product="DrillOps",
                                    derived_from_resolution="inv_1")
    action = hitl.propose(article, "knowledge_article_draft", ActionType.KB_PUBLISH_ARTICLE.value,
                          {"audience": "internal", "title": article.title.text})
    return _proposal(hitl, action)


def build_all_surfaces() -> dict[str, list[dict[str, Any]]]:
    """Every builder, in every mode (decision / display, empty / populated) — the contract-test corpus."""
    from pm_agent.a2ui import surfaces  # pylint: disable=import-outside-toplevel

    ado, figma, kb = ado_proposal(), figma_proposal(), kb_proposal()
    draft = requirement_draft()
    failed = report_for(draft, "requirement_draft", overall=VerificationOutcome.FAIL, blocked=("clm_removed_1",))
    title_diff = {"before": {"System.Title": "Old title"}, "after": ado.action.payload["fields"]}
    article = knowledge_article()
    return {
        "approval_ado": surfaces.approval_surface(ado.action, ado.subject, ado.report, diff=title_diff),
        "approval_ado_no_diff": surfaces.approval_surface(ado.action, ado.subject, ado.report),
        "approval_figma": surfaces.approval_surface(figma.action, figma.subject, figma.report),
        "approval_kb": surfaces.approval_surface(kb.action, kb.subject, kb.report),
        "requirement_review": surfaces.requirement_review_surface(draft, failed, template_fields=TEMPLATE_FIELDS),
        "requirement_review_unverified": surfaces.requirement_review_surface(draft, None),
        "requirement_review_decision": surfaces.requirement_review_surface(ado.subject, ado.report,  # type: ignore[arg-type]
                                                                           action=ado.action),
        "readiness": surfaces.readiness_surface(PROBLEM_ID, readiness(), questions()),
        "duplicates": surfaces.duplicate_decision_surface(PROBLEM_ID, duplicate_candidates()),
        "problem_detail": surfaces.problem_detail_surface(product_problem(), signals()),
        "problem_inbox": surfaces.problem_inbox_surface([product_problem(), second_problem()], SOURCE_HEALTH),
        "problem_inbox_empty": surfaces.problem_inbox_surface([], {}),
        "prototype": surfaces.prototype_surface(display_prototype_spec(), None, IMAGES),
        "prototype_decision": surfaces.prototype_surface(figma.subject, figma.report, IMAGES,  # type: ignore[arg-type]
                                                         action=figma.action),
        "knowledge_review": surfaces.knowledge_review_surface(article, report_for(article, "knowledge_article_draft")),
        "knowledge_review_decision": surfaces.knowledge_review_surface(kb.subject, kb.report,  # type: ignore[arg-type]
                                                                       action=kb.action),
        "investigation": surfaces.investigation_surface(investigation_pack()),
        "audit": surfaces.audit_surface(ledger_entries()),
        "audit_empty": surfaces.audit_surface([]),
        "status": surfaces.status_surface("status-run#42", "Drafting requirement", ["Collecting evidence", "Verifying"]),
    }


# --------------------------------------------------------------------------------------- inspection helpers
def create_surface(messages: list[dict[str, Any]]) -> dict[str, Any]:
    """The single ``createSurface`` payload a builder returns (builders emit one self-contained message)."""
    assert len(messages) == 1, messages
    assert set(messages[0]) == {"version", "createSurface"}, messages[0].keys()
    payload: dict[str, Any] = messages[0]["createSurface"]
    return payload


def components(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return list(create_surface(messages)["components"])


def component_map(messages: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {c["id"]: c for c in components(messages)}


def data_model(messages: list[dict[str, Any]]) -> dict[str, Any]:
    return dict(create_surface(messages)["dataModel"])


def of_type(messages: list[dict[str, Any]], name: str) -> list[dict[str, Any]]:
    return [c for c in components(messages) if c["component"] == name]


def resolve_pointer(document: Any, pointer: str) -> Any:
    """RFC 6901 JSON Pointer resolution (absolute pointers only)."""
    assert pointer.startswith("/"), pointer
    node = document
    for raw in pointer[1:].split("/") if pointer != "/" else []:
        token = raw.replace("~1", "/").replace("~0", "~")
        node = node[int(token)] if isinstance(node, list) else node[token]
    return node


def resolve(value: Any, model: dict[str, Any]) -> Any:
    """Replace every ``{"@path": …}`` binding inside *value* with the data-model value it points at."""
    if isinstance(value, dict):
        if set(value) == {"@path"}:
            return resolve_pointer(model, value["@path"])
        return {k: resolve(v, model) for k, v in value.items()}
    if isinstance(value, list):
        return [resolve(v, model) for v in value]
    return value


def bound_paths(value: Any) -> Iterator[str]:
    if isinstance(value, dict):
        if "@path" in value:
            yield value["@path"]
        for item in value.values():
            yield from bound_paths(item)
    elif isinstance(value, list):
        for item in value:
            yield from bound_paths(item)


def buttons(messages: list[dict[str, Any]], event: str | None = None) -> list[dict[str, Any]]:
    found = of_type(messages, "Button")
    return [b for b in found if event is None or b["action"]["event"]["name"] == event]


def events(messages: list[dict[str, Any]]) -> list[str]:
    return [b["action"]["event"]["name"] for b in buttons(messages)]


_MD_ESCAPE = re.compile(r"\\([\\`*_{}\[\]()#+\-.!|<>~&])")


def unescape_md(text: str) -> str:
    return _MD_ESCAPE.sub(r"\1", text)


def texts(messages: list[dict[str, Any]]) -> list[str]:
    """Literal ``Text`` contents with Markdown escapes removed (what a user reads)."""
    return [unescape_md(c["text"]) for c in of_type(messages, "Text") if isinstance(c["text"], str)]


def all_text(messages: list[dict[str, Any]]) -> str:
    return "\n".join(texts(messages))


def button_label(messages: list[dict[str, Any]], button: dict[str, Any]) -> str:
    return unescape_md(str(component_map(messages)[button["child"]]["text"]))


def slb_components(messages: list[dict[str, Any]], name: str | None = None) -> list[dict[str, Any]]:
    from pm_agent.a2ui import SLB_CATALOG_ID  # pylint: disable=import-outside-toplevel

    return [c for c in components(messages)
            if c.get("@catalogId") == SLB_CATALOG_ID and (name is None or c["component"] == name)]


def click(messages: list[dict[str, Any]], button: dict[str, Any],
          timestamp: str = "2026-11-24T10:00:00Z") -> dict[str, Any]:
    """Simulate the renderer: resolve the button's event context against the data model → v1.0 ``action``."""
    model = data_model(messages)
    event = button["action"]["event"]
    action: dict[str, Any] = {"name": event["name"], "surfaceId": create_surface(messages)["surfaceId"],
                              "sourceComponentId": button["id"], "timestamp": timestamp,
                              "context": resolve(event.get("context", {}), model)}
    if "userMessage" in event:
        action["userMessage"] = resolve(event["userMessage"], model)
    return {"version": "1.0", "action": action}


def canonical(messages: list[dict[str, Any]]) -> str:
    return json.dumps(messages, sort_keys=True, ensure_ascii=False)
