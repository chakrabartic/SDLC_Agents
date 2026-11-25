"""W4 prototype, W6 knowledge and the operations capabilities through the core app (AC4–AC7)."""
from __future__ import annotations

from datetime import UTC, datetime

import pytest

from pm_agent.agents.offline import ScriptedModelProvider
from pm_agent.connectors.base import make_signal
from pm_agent.core_app import build_core_app
from pm_agent.grounding.chunking import chunk_signal
from pm_agent.hitl.resume import decision_resume
from pm_agent.prototype import compile_plugin_program
from pm_agent.schemas import Approver, Decision, LinkType, PrototypeSpec, RecordType, SourceSystem
from pm_agent.workflows.prototype import claims_by_id, committed_requirement
from tests.component.helpers import AppSession
from tests.component.test_define_requirement import (
    FIRST_SENTENCE,
    PM,
    ROOT_CAUSE,
    SCRIPT,
    _cite,
    _run_to_approval,
    _services_with_problem,
)
from tests.support import PD_CLAIM, PD_QUOTE, UX

pytestmark = pytest.mark.component

RESOLUTION = "Re-uploading the mapping file and re-running standardization restored TVDHD in TIME_SURFACE."
KB_REVIEWER = Approver(sub="kb-1@slb.test", email="EMAIL_KB0001", idp="entra-test", groups=["product_kb_reviewers"])

PROTOTYPE = {"screens": [{"name": "Channel mapping status", "width": 1440, "height": 900, "nodes": [
    {"kind": "frame", "name": "Page"},
    {"kind": "instance", "name": "Shell", "parent": 0, "component_key": "dls:app-shell/default"},
    {"kind": "instance", "name": "Missing channel warning", "parent": 0, "component_key": "dls:banner/warning"},
    {"kind": "text", "name": "Problem summary", "parent": 0, "text_claim": 0},
    {"kind": "instance", "name": "Track chart", "parent": 0, "component_key": "dls:chart/line"},
    {"kind": "instance", "name": "Re-run standardization", "parent": 0, "component_key": "dls:button/primary",
     "variant_props": {"size": "md"}}]}], "new_patterns": []}


def _kb_article(request: str) -> dict:
    import json  # pylint: disable=import-outside-toplevel
    resolution_id = next(e["evidence_id"] for e in json.loads(request)["evidence"] if "Re-uploading" in e["text"])
    return {"title": _cite(FIRST_SENTENCE), "symptom": [_cite(PD_CLAIM, quote=PD_QUOTE)],
            "root_cause": [_cite("The mapping file was uploaded after the channel streamed.", quote=ROOT_CAUSE)],
            "resolution": [_cite(RESOLUTION, evidence_id=resolution_id)], "workaround": []}


async def _committed_requirement():  # type: ignore[no-untyped-def]
    services, problem = _services_with_problem()
    session, paused, approval = await _run_to_approval(services, problem)
    action_id = approval.payload["action_id"]
    services.approval_service.decide(action_id=action_id, decision=Decision.APPROVE, assertion=services.identity.issue(PM))
    await session.send(decision_resume(approval.interrupt_id, action_id=action_id, decision="approve"),
                       invocation_id=paused.invocation_id)
    return services, problem


async def _approve_and_resume(services, session, turn, *people):  # type: ignore[no-untyped-def]
    [pause] = turn.interrupts
    action_id = pause.payload["action_id"]
    for person in people:
        services.approval_service.decide(action_id=action_id, decision=Decision.APPROVE,
                                  assertion=services.identity.issue(person))
    return await session.send(decision_resume(pause.interrupt_id, action_id=action_id, decision="approve"),
                              invocation_id=turn.invocation_id)


async def test_prototype_reuses_dls_and_needs_pm_and_ux_before_rendering() -> None:
    services, problem = await _committed_requirement()
    draft = committed_requirement(services, problem.problem_id)
    assert draft is not None
    session = AppSession(build_core_app(services, ScriptedModelProvider(dict(SCRIPT, prototype_composer=[PROTOTYPE]))))
    turn = await session.send({"capability": "design_prototype", "problem_id": problem.problem_id})
    [pause] = turn.interrupts
    action = services.approvals.get_action(pause.payload["action_id"])
    assert action.payload["reuse_ratio"] == 1.0 and "dls:banner/warning" in action.payload["component_keys"]
    services.approval_service.decide(action_id=action.action_id, decision=Decision.APPROVE,
                              assertion=services.identity.issue(PM))
    renderer = services.write_backends["svc-prototype-renderer"]
    assert renderer.calls == []  # PM alone is not enough (two-person rule)
    final = await _approve_and_resume(services, session, turn, UX)
    assert final.output_of("design_prototype")["status"] == "committed"
    assert len(renderer.creates()) == 1
    _, spec = services.subjects.get(final.output_of("design_prototype")["spec_ref"])
    assert isinstance(spec, PrototypeSpec)
    program = compile_plugin_program(spec, claims_by_id(draft))
    assert 'figma.importComponentByKeyAsync("dls:banner/warning")' in program
    assert FIRST_SENTENCE.replace('"', '\\"') in program  # text only from verified requirement claims


async def test_prototype_requires_an_approved_requirement() -> None:
    services, problem = _services_with_problem()
    session = AppSession(build_core_app(services, ScriptedModelProvider(SCRIPT)))
    turn = await session.send({"capability": "design_prototype", "problem_id": problem.problem_id})
    assert turn.output_of("design_prototype")["status"] == "requirement_not_approved"


async def test_knowledge_article_publishes_only_after_kb_reviewer_approval_and_traces() -> None:
    services, problem = await _committed_requirement()
    note = make_signal(redactor=services.redactor, source_system=SourceSystem.SERVICENOW,
                       source_record_id="CS0000999", source_version="1", record_type=RecordType.JOURNAL_NOTE,
                       permalink="https://servicenow.example.invalid/CS0000999", raw_text=f"Resolution: {RESOLUTION}",
                       captured_utc=datetime(2026, 5, 12, tzinfo=UTC))
    services.evidence.add_signal(note)
    for chunk in chunk_signal(note):
        services.evidence.add_chunk(chunk)
    services.problems.link(problem.problem_id, note.signal_id, LinkType.HARD_REF, 1.0, "test")
    session = AppSession(build_core_app(services, ScriptedModelProvider(dict(SCRIPT, kb_writer=[_kb_article]))))
    turn = await session.send({"capability": "capture_knowledge", "problem_id": problem.problem_id})
    kb = services.write_backends["mcp-kb-write"]
    assert turn.interrupts and kb.calls == []
    final = await _approve_and_resume(services, session, turn, KB_REVIEWER)
    assert final.output_of("capture_knowledge")["status"] == "committed" and len(kb.creates()) == 1

    trace = await AppSession(build_core_app(services, ScriptedModelProvider(SCRIPT))).send(
        {"capability": "trace_roadmap", "problem_id": problem.problem_id})
    link_types = {link["link_type"] for link in trace.output_of("trace_roadmap")["links"]}
    assert {"signal_of", "specified_by", "captured_as_knowledge"} <= link_types


async def test_investigation_and_trends_are_cited() -> None:
    services, problem = _services_with_problem()
    app = build_core_app(services, ScriptedModelProvider(SCRIPT))
    pack = (await AppSession(app).send({"capability": "investigate_issue",
                                        "request": "TVDHD missing from TIME_SURFACE"})).output_of("investigate_issue")
    assert pack["status"] == "ok" and pack["pack"]["timeline"]
    assert all(claim["citations"] for claim in pack["pack"]["timeline"])
    trends = (await AppSession(app).send({"capability": "surface_trends"})).output_of("surface_trends")
    assert trends["advisory"] is True and trends["top_problems"][0]["problem_id"] == problem.problem_id
