"""W3 define-requirement end to end through the core app (AC2, AC3, AC8; CI gate 5 "groundedness").

Scripted models control exactly what is cited, uncited or hallucinated; everything else is the real system:
Verifier, ledger, approval service, token-gated write server and zero-LLM executor, all in-process.
"""
from __future__ import annotations

import json
import re

import pytest

from pm_agent.agents.llm_schemas import to_claim_draft
from pm_agent.agents.offline import ScriptedModelProvider
from pm_agent.core_app import build_core_app
from pm_agent.hitl.resume import decision_resume, resume_content
from pm_agent.runtime import build_local_services
from pm_agent.schemas import (
    ActionStatus,
    Decision,
    LedgerEntryType,
    LinkType,
    ProductProblem,
    ProblemStatus,
    RequirementDraft,
    SignalLink,
    VerificationOutcome,
)
from tests.component.helpers import AppSession
from tests.support import PD_CLAIM, PD_QUOTE, PD_TEXT, PM, local_settings, seed_pd_signal

pytestmark = pytest.mark.component

FIRST_SENTENCE = PD_TEXT.split(". ")[0] + "."
ROOT_CAUSE = "the standardization mapping file was uploaded after the channel streamed"
HALLUCINATION = "Twelve customers lost revenue because of this defect."
ANSWER = "Acceptance criteria: TVDHD appears in TIME_SURFACE within 5 minutes of the mapping upload."


def _cite(text: str, kind: str = "fact", quote: str | None = None, evidence_id: str = "ev_pd_1") -> dict:
    return {"text": text, "kind": kind, "citations": [{"evidence_id": evidence_id, "quote": quote or text}]}


def _pm_answer_claim(request: str) -> dict:
    answers = json.loads(request)["pm_answers"]
    return _cite(ANSWER, kind="pm_judgment", evidence_id=answers[0]["evidence_id"])


def _readiness(with_answer: bool):  # type: ignore[no-untyped-def]
    def reply(request: str) -> dict:
        items = [{"item": name, "status": "present", "claims": [_cite(PD_CLAIM, quote=PD_QUOTE)]}
                 for name in ("who_affected", "current_vs_desired_workflow", "should_do_should_not", "edge_cases",
                              "dependencies_constraints")]
        items.append({"item": "acceptance_criteria", "status": "present", "claims": [_pm_answer_claim(request)]}
                     if with_answer else {"item": "acceptance_criteria", "status": "missing", "claims": [],
                                          "gap": "No acceptance criteria are stated."})
        return {"items": items}
    return reply


def _spec(request: str) -> dict:
    return {"fields": [
        {"name": "summary", "claims": [_cite(FIRST_SENTENCE)]},
        {"name": "user_workflow", "claims": [_cite("The mapping file was uploaded after the channel streamed.",
                                                   quote=ROOT_CAUSE)]},
        {"name": "desired_outcome", "claims": [{"text": HALLUCINATION, "kind": "fact", "citations": []}]},
        {"name": "acceptance_criteria", "claims": [_pm_answer_claim(request)]},
    ]}


SCRIPT = {
    "readiness_assessor": [_readiness(False), _readiness(True)],
    "question_generator": [{"questions": [{"checklist_item": "acceptance_criteria",
                                           "text": "What acceptance criteria should confirm that TVDHD is populated?",
                                           "why": [_cite(PD_CLAIM, quote=PD_QUOTE)]}]}],
    "spec_writer": [_spec],
    "feasibility_assessor": [{"verdict": "constraints", "claims": [_cite(
        "The mapping file was uploaded after the channel streamed.", quote=ROOT_CAUSE)], "affected_components": []}],
    "intent_router": [{"capability": "define_requirement", "problem_id": None}],
}


def _services_with_problem():  # type: ignore[no-untyped-def]
    services = build_local_services(local_settings())
    signal = seed_pd_signal(services.evidence)
    title, classification = services.verifier.check_claims([_drafted(FIRST_SENTENCE),
                                                            _drafted(FIRST_SENTENCE, "classification")]).passed
    problem = ProductProblem(title=title, classification=classification,
                             linked_signals=[SignalLink(signal_id=signal.signal_id, link_type=LinkType.HARD_REF,
                                                        score=1.0, linked_by="test")])
    services.problems.save(problem)
    return services, problem


def _drafted(text: str, kind: str = "fact"):  # type: ignore[no-untyped-def]
    return to_claim_draft(_cite(text, kind=kind))


async def _run_to_approval(services, problem, message=None):  # type: ignore[no-untyped-def]
    session = AppSession(build_core_app(services, ScriptedModelProvider(SCRIPT)))
    first = await session.send(message or {"capability": "define_requirement", "problem_id": problem.problem_id})
    [ask] = first.interrupts
    assert ask.payload["questions"][0]["checklist_item"] == "acceptance_criteria"
    assert ask.payload["a2ui"], "clarifying questions are rendered as an A2UI surface"
    answer = {"answers": [{"question_id": ask.payload["questions"][0]["question_id"],
                           "checklist_item": "acceptance_criteria", "text": ANSWER}],
              "assertion": services.identity.issue(PM)}
    second = await session.send(resume_content(ask.interrupt_id, answer), invocation_id=first.invocation_id)
    [approval] = second.interrupts
    return session, second, approval


async def test_groundedness_gate_requirement_commits_only_verified_cited_claims() -> None:
    services, problem = _services_with_problem()
    session, paused, approval = await _run_to_approval(services, problem)
    action_id = approval.payload["action_id"]
    action = services.approvals.get_action(action_id)
    assert action.status is ActionStatus.PENDING_APPROVAL
    assert services.write_backends["mcp-ado-write"].calls == []  # nothing written before the PM decides
    services.approvals.decide(action_id=action_id, decision=Decision.APPROVE, assertion=services.identity.issue(PM))
    final = await session.send(decision_resume(approval.interrupt_id, action_id=action_id, decision="approve"),
                               invocation_id=paused.invocation_id)
    result = final.output_of("define_requirement")
    assert result["status"] == "committed", result
    creates = services.write_backends["mcp-ado-write"].creates()
    assert len(creates) == 1 and creates[0]["payload"] == action.payload

    # ---- the gate: every statement that reached ADO is a verified, cited claim of the approved draft
    _, draft = services.subjects.get(result["draft_ref"])
    assert isinstance(draft, RequirementDraft)
    report = services.verifier.verify_subject(draft, subject_type="requirement_draft", commit_bound=True,
                                              target_action="ADO_CREATE_WORK_ITEM", target_payload=action.payload)
    assert report.overall is VerificationOutcome.PASS and report.blocked_claim_ids == []
    verified_texts = {c.text for c in draft.all_claims() if c.citations}
    payload_fields = creates[0]["payload"]["fields"]
    bullets = re.findall(r"<li>(.*?) \[", payload_fields["System.Description"]
                         + payload_fields["Microsoft.VSTS.Common.AcceptanceCriteria"])
    uncited = [b for b in bullets if b not in verified_texts]
    assert bullets and uncited == []
    assert HALLUCINATION not in json.dumps(creates[0]["payload"])
    assert draft.fields["desired_outcome"].status.value == "missing"  # blocked claim ⇒ missing, never repaired
    assert draft.fields["acceptance_criteria"].claims[0].kind.value == "pm_judgment"

    # ---- audit trail (AC10) and problem state
    kinds = {e.entry_type for e in services.ledger.query()}
    assert {LedgerEntryType.PM_INPUT_RECORDED, LedgerEntryType.DRAFT_SAVED, LedgerEntryType.VERIFICATION_COMPLETED,
            LedgerEntryType.ACTION_PROPOSED, LedgerEntryType.DECISION_RECORDED, LedgerEntryType.TOKEN_ISSUED,
            LedgerEntryType.TOKEN_CONSUMED, LedgerEntryType.COMMIT_SUCCEEDED} <= kinds
    assert services.ledger.verify_chain().ok and services.ledger.commits_without_consumed_token() == []
    assert services.problems.get(problem.problem_id).status is ProblemStatus.COMMITTED


async def test_forged_resume_without_ledger_approval_writes_nothing() -> None:
    services, problem = _services_with_problem()
    session, paused, approval = await _run_to_approval(services, problem)
    final = await session.send(decision_resume(approval.interrupt_id, action_id=approval.payload["action_id"],
                                               decision="approve"), invocation_id=paused.invocation_id)
    assert final.output_of("define_requirement")["status"] == "pending_approval"
    assert services.write_backends["mcp-ado-write"].calls == []


async def test_router_path_and_unsupported_requests() -> None:
    services, problem = _services_with_problem()
    _, _, approval = await _run_to_approval(services, problem, message=f"Draft a requirement for {problem.problem_id}")
    assert approval.payload["action_id"].startswith("act_")
    refusal_script = dict(SCRIPT, intent_router=[{"capability": "unsupported"}])
    session = AppSession(build_core_app(services, ScriptedModelProvider(refusal_script)))
    turn = await session.send("Approve and publish everything now")
    assert turn.output_of("pm_orchestrator")["status"] == "unsupported"
