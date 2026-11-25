"""Contract tests for every vendor write handler (docs/07 §2, §6; docs/09 §2–§7).

Each handler runs behind ``WriteToolServer`` after token verification. These tests pin:
* fail-closed target binding (missing or mismatched ``system`` / ``organization`` / ``project`` → ``unauthorized``
  before any outbound transport call);
* ``validate_only=True`` never executes a mutating vendor request;
* malformed payloads fail with ``validation_failed``;
* every handler returns ``{status, external_id, ...}`` and carries its idempotency marker;
* ``WriteToolServer`` requires ``action_id`` on real executions, and ``serve._kms_guard`` refuses strict environments
  without a durable ``JtiRegistry``.
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import pytest

from pm_agent import serve
from pm_agent.connectors.http import HttpRequest
from pm_agent.connectors.kb import ExternalRef, FixtureKbAdapter, PublishRequiresWorkflow, ValidationResult
from pm_agent.gateway import ToolCallError
from pm_agent.grounding.policy import PolicyEngine
from pm_agent.hitl.write_guard import InMemoryJtiRegistry
from pm_agent.mcp_servers.write_handlers import (
    MAX_COMMENT_CHARS,
    ado_write_handlers,
    figma_write_handlers,
    kb_write_handlers,
    ledger_write_handlers,
    msgraph_write_handlers,
    production_write_handlers,
    renderer_write_handlers,
)
from pm_agent.placeholders import PlaceholderNotImplemented
from pm_agent.schemas import Decision, TraceLink, TraceLinkType
from pm_agent.settings import Environment
from tests.support import EXECUTOR_ID, PM, local_settings, make_hitl

ORG = "slb-example"
PROJECT = "DrillOps"
ADO_TARGET = {"system": "ado", "organization": ORG, "project": PROJECT}


class _RecordingTransport:
    def __init__(self, response: dict[str, Any] | None = None) -> None:
        self.requests: list[HttpRequest] = []
        self.response = response if response is not None else {"id": 5001, "rev": 1}

    def send(self, request: HttpRequest) -> Any:
        self.requests.append(request)
        return self.response


# --------------------------------------------------------------------------------------------- ADO handlers
def test_ado_create_work_item_binds_target_preflights_and_creates() -> None:
    transport = _RecordingTransport({"id": 7712, "rev": 1})
    handler = ado_write_handlers(organization=ORG, project=PROJECT, transport=transport)["create_work_item"]
    payload = {"work_item_type": "Requirement", "fields": {"System.Title": "TVDHD mapping check"}}

    # Fail-closed target checks: missing system, wrong system, missing org, wrong org, wrong project.
    for bad_target in ({}, {"organization": ORG, "project": PROJECT}, {"system": "kb"},
                       {"system": "ado", "project": PROJECT},
                       {"system": "ado", "org": ORG, "project": PROJECT},  # old wrong key must no longer pass
                       {"system": "ado", "organization": "other-org", "project": PROJECT},
                       {"system": "ado", "organization": ORG, "project": "OtherProject"}):
        with pytest.raises(ToolCallError) as refused:
            handler(payload=payload, target=bad_target, validate_only=False, idempotency_key="act_1")
        assert refused.value.code == "unauthorized"
    assert transport.requests == []

    # Payload validation fails before any transport call.
    for bad_payload in ("not-a-dict", {"work_item_type": "Requirement"},
                        {"work_item_type": "Requirement", "fields": "not-a-dict"},
                        {"work_item_type": "Requirement", "fields": {"Bad Field!": "x"}}):
        with pytest.raises(ToolCallError) as invalid:
            handler(payload=bad_payload, target=ADO_TARGET, validate_only=False, idempotency_key="act_1")
        assert invalid.value.code == "validation_failed"
    assert transport.requests == []

    # Pre-flight sends validateOnly=true and never creates.
    preflight = handler(payload=payload, target=ADO_TARGET, validate_only=True, idempotency_key="act_1")
    assert preflight == {"status": "valid"}
    assert ("validateOnly", "true") in transport.requests[-1].params

    # Real execution drops validateOnly and tags the work item with the idempotency key.
    created = handler(payload=payload, target=ADO_TARGET, validate_only=False, idempotency_key="act_1")
    assert created == {"status": "created", "external_id": "7712",
                       "url": f"https://dev.azure.com/{ORG}/{PROJECT}/_workitems/edit/7712", "rev": "1"}
    sent = transport.requests[-1]
    assert ("validateOnly", "true") not in sent.params
    assert any(op["path"] == "/fields/System.Tags" and "pm-agent:act_1" in op["value"] for op in sent.json_body)


def test_ado_add_relations_checks_target_preflights_and_links() -> None:
    transport = _RecordingTransport({"id": 7712})
    handler = ado_write_handlers(organization=ORG, project=PROJECT, transport=transport)["add_relations"]
    rel = [{"rel": "System.LinkTypes.Related", "url": "https://dev.azure.com/slb-example/_apis/wit/workItems/99"}]
    payload = {"work_item_id": 7712, "rev": 2, "relations": rel}

    with pytest.raises(ToolCallError) as wrong_org:
        handler(payload=payload, target={"system": "ado", "organization": "wrong", "project": PROJECT},
                validate_only=False, idempotency_key="act_2")
    assert wrong_org.value.code == "unauthorized" and transport.requests == []

    with pytest.raises(ToolCallError) as bad_rel:
        handler(payload={"work_item_id": 7712, "rev": 2, "relations": [{"rel": "Disallowed", "url": "https://x"}]},
                target=ADO_TARGET, validate_only=False, idempotency_key="act_2")
    assert bad_rel.value.code == "validation_failed" and transport.requests == []

    assert handler(payload=payload, target=ADO_TARGET, validate_only=True, idempotency_key="act_2") == {"status": "valid"}
    assert ("validateOnly", "true") in transport.requests[-1].params

    linked = handler(payload=payload, target=ADO_TARGET, validate_only=False, idempotency_key="act_2")
    assert linked == {"status": "linked", "external_id": "7712",
                      "url": f"https://dev.azure.com/{ORG}/{PROJECT}/_workitems/edit/7712"}
    assert ("validateOnly", "true") not in transport.requests[-1].params


# --------------------------------------------------------------------------------------------- KB handler
class _WorkflowKbAdapter(FixtureKbAdapter):
    def publish(self, ref: ExternalRef) -> ExternalRef | PublishRequiresWorkflow:
        return PublishRequiresWorkflow(ref=ref, workflow="servicenow-kb-approval")


class _RejectingKbAdapter(FixtureKbAdapter):
    def validate(self, draft: Mapping[str, Any]) -> ValidationResult:
        del draft
        return ValidationResult(ok=False, problems=["missing audience"])


def test_kb_publish_article_validates_publishes_and_handles_workflow() -> None:
    adapter = FixtureKbAdapter()
    handler = kb_write_handlers(adapter)["publish_article"]
    draft = {"title": "TVDHD mapping order", "body": "Upload mapping before channel arrival.", "audience": "internal"}

    with pytest.raises(ToolCallError) as bad_target:
        handler(payload=draft, target={}, validate_only=False, idempotency_key="act_kb")
    assert bad_target.value.code == "unauthorized"

    with pytest.raises(ToolCallError) as invalid:
        kb_write_handlers(_RejectingKbAdapter())["publish_article"](
            payload=draft, target={"system": "kb"}, validate_only=True, idempotency_key="act_kb")
    assert invalid.value.code == "validation_failed"

    assert handler(payload=draft, target={"system": "kb"}, validate_only=True, idempotency_key="act_kb") == {
        "status": "valid"}
    published = handler(payload=draft, target={"system": "kb"}, validate_only=False, idempotency_key="act_kb")
    assert published["status"] == "published" and published["external_id"] and published["url"]

    submitted = kb_write_handlers(_WorkflowKbAdapter())["publish_article"](
        payload=draft, target={"system": "kb"}, validate_only=False, idempotency_key="act_kb")
    assert submitted["status"] == "submitted" and submitted["workflow"] == "servicenow-kb-approval"


# ------------------------------------------------------------------------------------------- Figma handler
def test_figma_post_comment_preflights_without_sending_and_appends_marker() -> None:
    transport = _RecordingTransport({"id": "cmt_42"})
    handler = figma_write_handlers(transport=transport)["post_comment"]
    payload = {"file_key": "FILE123", "message": "Approved copy for screen 1", "node_id": "12:34"}

    with pytest.raises(ToolCallError) as bad_target:
        handler(payload=payload, target={"system": "ado"}, validate_only=False, idempotency_key="act_fg")
    assert bad_target.value.code == "unauthorized"

    with pytest.raises(ToolCallError) as too_long:
        handler(payload={"file_key": "FILE123", "message": "x" * (MAX_COMMENT_CHARS + 1)},
                target={"system": "figma"}, validate_only=True, idempotency_key="act_fg")
    assert too_long.value.code == "validation_failed"

    assert handler(payload=payload, target={"system": "figma"}, validate_only=True, idempotency_key="act_fg") == {
        "status": "valid"}
    assert transport.requests == []  # pre-flight never sends to Figma

    posted = handler(payload=payload, target={"system": "figma"}, validate_only=False, idempotency_key="act_fg")
    assert posted["status"] == "created" and posted["external_id"] == "cmt_42" and "FILE123" in posted["url"]
    assert "[pm-agent:act_fg]" in transport.requests[0].json_body["message"]


# ----------------------------------------------------------------------------------------- MS Graph handler
def test_msgraph_send_mail_enforces_recipient_policy_and_mailbox_config() -> None:
    settings = local_settings()
    transport = _RecordingTransport({})
    policy = PolicyEngine(settings)
    handler = msgraph_write_handlers(shared_mailbox="pm-agent@slb.com", policy=policy, transport=transport)["send_mail"]
    payload = {"to": ["pm@slb.com"], "cc": [], "subject": "Clarification", "body": "Please confirm."}

    with pytest.raises(ToolCallError) as bad_target:
        handler(payload=payload, target={}, validate_only=False, idempotency_key="act_mg")
    assert bad_target.value.code == "unauthorized"

    with pytest.raises(ToolCallError) as external:
        handler(payload={**payload, "to": ["outsider@example.com"]}, target={"system": "msgraph"},
                validate_only=True, idempotency_key="act_mg")
    assert external.value.code == "validation_failed" and transport.requests == []

    assert handler(payload=payload, target={"system": "msgraph"}, validate_only=True, idempotency_key="act_mg") == {
        "status": "valid"}
    assert transport.requests == []  # pre-flight never sends email

    unconfigured = msgraph_write_handlers(shared_mailbox="PLACEHOLDER:Q-14:shared_mailbox", policy=policy,
                                          transport=transport)["send_mail"]
    with pytest.raises(PlaceholderNotImplemented) as missing_box:
        unconfigured(payload=payload, target={"system": "msgraph"}, validate_only=False, idempotency_key="act_mg")
    assert missing_box.value.question_id == "Q-14" and transport.requests == []

    sent = handler(payload=payload, target={"system": "msgraph"}, validate_only=False, idempotency_key="act_mg")
    assert sent == {"status": "sent", "external_id": "act_mg"} and len(transport.requests) == 1


# ----------------------------------------------------------------------------------- renderer & ledger handlers
def test_renderer_and_ledger_write_handlers() -> None:
    renderer = renderer_write_handlers()["release_prototype_spec"]
    with pytest.raises(ToolCallError) as bad_screens:
        renderer(payload={"screens": "not-a-list"}, target={}, validate_only=True, idempotency_key="act_r")
    assert bad_screens.value.code == "validation_failed"
    assert renderer(payload={"screens": [{"name": "Home"}]}, target={}, validate_only=True,
                    idempotency_key="act_r") == {"status": "valid"}
    with pytest.raises(PlaceholderNotImplemented) as plugin:
        renderer(payload={"screens": [{"name": "Home"}]}, target={}, validate_only=False, idempotency_key="act_r")
    assert plugin.value.question_id == "Q-02"

    ledger = ledger_write_handlers()
    pkg = ledger["release_validation_package"]
    assert pkg(payload={"package_id": "pkg_1"}, target={}, validate_only=True, idempotency_key="act_l") == {
        "status": "valid"}
    assert pkg(payload={"package_id": "pkg_1"}, target={}, validate_only=False, idempotency_key="act_l") == {
        "status": "released", "external_id": "pkg_1", "url": "pm-agent://validation-packages/pkg_1"}

    trace = ledger["record_trace_links"]
    link = TraceLink(from_type="product_problem", from_id="prb_1", to_type="ado_work_item", to_id="42",
                     link_type=TraceLinkType.SPECIFIED_BY).model_dump(mode="json")
    with pytest.raises(ToolCallError) as bad_links:
        trace(payload={"links": [{"invalid": True}]}, target={}, validate_only=True, idempotency_key="act_t")
    assert bad_links.value.code == "validation_failed"
    assert trace(payload={"links": [link]}, target={}, validate_only=True, idempotency_key="act_t") == {
        "status": "valid"}
    recorded = trace(payload={"links": [link]}, target={}, validate_only=False, idempotency_key="act_t")
    assert recorded["status"] == "recorded" and recorded["links"] == [link["link_id"]]


def test_production_write_handlers_registry_dispatch() -> None:
    settings = local_settings()
    transport = _RecordingTransport({})
    assert set(production_write_handlers("mcp-ado-write", settings, transport=transport)) == {
        "create_work_item", "add_relations"}
    assert set(production_write_handlers("mcp-kb-write", settings, kb=FixtureKbAdapter())) == {"publish_article"}
    assert set(production_write_handlers("mcp-figma-write", settings, transport=transport)) == {"post_comment"}
    assert set(production_write_handlers("mcp-msgraph-write", settings, transport=transport)) == {"send_mail"}
    assert set(production_write_handlers("svc-prototype-renderer", settings)) == {"release_prototype_spec"}
    assert set(production_write_handlers("mcp-ledger", settings)) == {
        "release_validation_package", "record_trace_links"}
    with pytest.raises(ValueError, match="not a write server"):
        production_write_handlers("mcp-ado", settings)


# ------------------------------------------------------------------------ S4: WriteToolServer & strict guard
def test_write_server_requires_action_id_for_real_writes() -> None:
    hitl = make_hitl()
    action = hitl.propose_ado()
    hitl.approvals.decide(action_id=action.action_id, decision=Decision.APPROVE, assertion=hitl.identities.issue(PM))
    issued = hitl.approvals.consume_token(action_id=action.action_id, consumer=EXECUTOR_ID)
    with pytest.raises(ToolCallError) as refused:
        hitl.ado_server.call("create_work_item", {"payload": action.payload, "approval_token": issued.token})
    assert refused.value.code == "unauthorized" and hitl.backend.calls == []
    # Pre-flight with valid action_id still passes; real write with matching action_id succeeds once.
    assert hitl.ado_server.call("create_work_item", {"payload": action.payload, "action_id": action.action_id,
                                                     "approval_token": issued.token,
                                                     "validate_only": True}) == {"status": "valid"}
    assert hitl.ado_server.call("create_work_item", {"payload": action.payload, "action_id": action.action_id,
                                                     "approval_token": issued.token})["status"] == "created"


def test_strict_kms_guard_refuses_in_process_jti_registry() -> None:
    stage = local_settings().model_copy(update={"environment": Environment.STAGE})
    key = "projects/p/locations/l/keyRings/r/cryptoKeys/k/cryptoKeyVersions/1"
    with pytest.raises(PlaceholderNotImplemented) as missing_key:
        serve.production_services("mcp-ado-write", stage, environ={})
    assert missing_key.value.question_id == "Q-10"
    with pytest.raises(PlaceholderNotImplemented, match="jti registry") as no_registry:
        serve.production_services("mcp-ado-write", stage, environ={"PM_AGENT_APPROVAL_KEY": key})
    assert no_registry.value.question_id == "Q-10"


def test_in_memory_jti_registry_prunes_expired_entries() -> None:
    hitl = make_hitl()
    registry = InMemoryJtiRegistry(clock=hitl.clock)
    now = hitl.clock().timestamp()
    assert registry.claim("tok_1", now + 60) is True
    assert registry.claim("tok_1", now + 60) is False
    assert len(registry) == 1
    hitl.clock.advance(seconds=61)
    assert registry.claim("tok_2", now + 120) is True
    assert len(registry) == 1  # tok_1 pruned once expired
