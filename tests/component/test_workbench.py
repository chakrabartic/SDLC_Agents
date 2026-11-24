"""The PM Workbench BFF over HTTP: chat → clarifying answers → decision → commit (docs/11 §6, docs/07 §4)."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from pm_agent.agents.offline import ScriptedModelProvider
from pm_agent.workbench.app import IAP_HEADER, create_app
from tests.component.test_define_requirement import ANSWER, SCRIPT, _services_with_problem
from tests.support import OUTSIDER, PM

pytestmark = pytest.mark.component
CAPABILITIES = {"1.0": {"supportedCatalogIds": ["slb.com:pm_workbench_v1",
                                                "https://a2ui.org/specification/v1_0/catalogs/basic/catalog.json"]}}


def test_workbench_decision_path_goes_through_the_ledger_not_the_llm() -> None:
    services, problem = _services_with_problem()
    client = TestClient(create_app(services, ScriptedModelProvider(SCRIPT)))
    pm = {IAP_HEADER: services.identity.issue(PM)}
    assert client.post("/api/messages", json={"text": "hi"}).status_code == 401  # IAP identity required

    started = client.post("/api/messages", headers=pm, json={
        "capability": "define_requirement", "problem_id": problem.problem_id,
        "a2uiRendererCapabilities": CAPABILITIES}).json()
    [ask] = started["interrupts"]
    question = ask["payload"]["questions"][0]
    assert ask["a2ui"], "A2UI is forwarded to a renderer that declared capabilities"
    answered = client.post(f"/api/interrupts/{ask['interrupt_id']}/answer", headers=pm, json={
        "answers": [{"question_id": question["question_id"], "checklist_item": "acceptance_criteria",
                     "text": ANSWER}]}).json()
    [approval] = answered["interrupts"]
    assert approval["a2ui"] == []  # no capabilities declared on this turn ⇒ text-only fallback
    action_id = approval["payload"]["action_id"]

    assert client.post(f"/api/actions/{action_id}/decide", json={"decision": "approve"}).status_code == 401
    outsider = {IAP_HEADER: services.identity.issue(OUTSIDER)}
    assert client.post(f"/api/actions/{action_id}/decide", headers=outsider,
                       json={"decision": "approve"}).status_code == 403
    assert services.write_backends["mcp-ado-write"].calls == []

    decided = client.post(f"/api/actions/{action_id}/decide", headers=pm, json={"decision": "approve"}).json()
    assert decided["token_issued"] and decided["workflow"]["result"]["status"] == "committed"
    assert len(services.write_backends["mcp-ado-write"].creates()) == 1
    audit = client.get("/api/audit", params={"action_id": action_id}, headers=pm).json()
    assert audit["chain_ok"] and audit["entries"][-1]["entry_type"] == "COMMIT_SUCCEEDED"


def _button(messages: list, event_name: str) -> tuple[str, str, dict]:  # type: ignore[type-arg]
    """(surfaceId, componentId, resolved context) of the first button emitting *event_name*."""
    for message in messages:
        create = message.get("createSurface") or {}
        data = create.get("dataModel") or {}
        for component in create.get("components", []):
            event = ((component.get("action") or {}).get("event") or {})
            if event.get("name") == event_name:
                context = {}
                for key, value in (event.get("context") or {}).items():
                    if isinstance(value, dict) and "@path" in value:
                        node = data
                        for part in value["@path"].strip("/").split("/"):
                            node = node.get(part, "") if isinstance(node, dict) else ""
                        value = node
                    context[key] = value
                return create["surfaceId"], component["id"], context
    raise AssertionError(f"no {event_name} button")


def test_a2ui_decision_event_is_bound_to_the_payload_the_pm_saw() -> None:
    services, problem = _services_with_problem()
    client = TestClient(create_app(services, ScriptedModelProvider(SCRIPT)))
    pm = {IAP_HEADER: services.identity.issue(PM)}
    started = client.post("/api/messages", headers=pm, json={
        "capability": "define_requirement", "problem_id": problem.problem_id,
        "a2uiRendererCapabilities": CAPABILITIES}).json()
    [ask] = started["interrupts"]
    surface_id, component_id, context = _button(ask["a2ui"], "answer_questions")
    question_id = ask["payload"]["questions"][0]["question_id"]
    answer_event = {"message": {"version": "1.0", "action": {
        "name": "answer_questions", "surfaceId": surface_id, "sourceComponentId": component_id,
        "timestamp": "2026-11-24T10:00:00Z", "context": {**context, "answers": {question_id: ANSWER}}}},
        "a2uiRendererCapabilities": CAPABILITIES}
    answered = client.post("/api/ui-actions", headers=pm, json=answer_event).json()
    [approval] = answered["interrupts"]
    surface_id, component_id, context = _button(approval["a2ui"], "approve_action")
    assert context["actionId"] == approval["payload"]["action_id"] and context["payloadSha256"].startswith("sha256:")

    def approve(ctx: dict) -> object:  # type: ignore[type-arg]
        return client.post("/api/ui-actions", headers=pm, json={"message": {"version": "1.0", "action": {
            "name": "approve_action", "surfaceId": surface_id, "sourceComponentId": component_id,
            "timestamp": "2026-11-24T10:05:00Z", "context": ctx}}})

    assert approve({**context, "payloadSha256": "sha256:" + "0" * 64}).status_code == 409  # not what was shown
    assert approve({**context, "actionVersion": context["actionVersion"] + 5}).status_code == 409  # stale card
    decided = approve(context).json()
    assert decided["workflow"]["result"]["status"] == "committed"
    assert client.post("/api/ui-actions", headers=pm, json={"message": {"version": "1.0", "action": {
        "name": "approve_action", "surfaceId": surface_id, "sourceComponentId": component_id,
        "timestamp": "not-a-time", "context": context}}}).status_code == 400
