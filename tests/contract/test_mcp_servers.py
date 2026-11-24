"""MCP server contract (docs/09 §1, docs/08 §3): registry ⇔ Agent Registry toolspecs, truthful annotations, and
token-gated write tools — exercised through the built ``MCPServer`` with the SDK's in-process client.

Agent Gateway CEL policies key on ``mcp.tool.isReadOnly`` and on exact tool names, so a drift between the code, the
Terraform toolspecs and the action catalog is a security bug, not a cosmetic one: every mismatch is printed.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from mcp import Client
from starlette.testclient import TestClient

from pm_agent.connectors.ado import HttpAdoConnector
from pm_agent.connectors.fixtures import golden_connectors
from pm_agent.hitl.catalog import ActionCatalog
from pm_agent.mcp_servers.factory import build_server, http_app
from pm_agent.mcp_servers.local import local_services
from pm_agent.mcp_servers.registry import (
    INTERNAL_SERVERS,
    READ_SERVERS,
    SERVER_TOOLS,
    TOKEN_GATED_TOOLS,
    WRITE_SERVERS,
    ToolSpec,
)
from pm_agent.mcp_servers.services import ServerServices
from pm_agent.mcp_servers.write_handlers import ledger_write_handlers
from pm_agent.mcp_servers.write_server import WriteToolServer
from pm_agent.schemas import Decision, LedgerEntryType
from pm_agent.settings import Settings, load_settings
from tests.support import EXECUTOR_ID, PM, ado_payload, grounded_draft, make_hitl

pytestmark = pytest.mark.contract

TOOLSPEC_DIR = Path(__file__).resolve().parents[2] / "infra" / "terraform" / "modules" / "agent_registry" / "toolspecs"
HINTS = (("read_only", "readOnlyHint"), ("destructive", "destructiveHint"), ("idempotent", "idempotentHint"),
         ("open_world", "openWorldHint"))


@pytest.fixture(name="settings", scope="module")
def _settings() -> Settings:
    return load_settings(environ={"PM_AGENT_ENV": "local"})


@pytest.fixture(name="services", scope="module")
def _services(settings: Settings) -> ServerServices:
    return local_services(settings)


def _toolspecs() -> dict[str, dict[str, dict[str, Any]]]:
    specs: dict[str, dict[str, dict[str, Any]]] = {}
    for path in sorted(TOOLSPEC_DIR.glob("*.json")):
        specs[path.stem] = {tool["name"]: tool for tool in json.loads(path.read_text(encoding="utf-8"))["tools"]}
    return specs


async def _live_tools(services: ServerServices, name: str) -> dict[str, Any]:
    async with Client(build_server(name, services)) as client:
        return {tool.name: tool for tool in (await client.list_tools()).tools}


# --------------------------------------------------------------------------------------- registry ⇔ toolspecs
def test_registry_matches_agent_registry_toolspecs() -> None:
    toolspecs = _toolspecs()
    mismatches: list[str] = []
    for server in sorted(set(SERVER_TOOLS) | set(toolspecs)):
        ours = {spec.name: spec for spec in SERVER_TOOLS.get(server, [])}
        theirs = toolspecs.get(server, {})
        if server not in toolspecs:
            mismatches.append(f"{server}: in registry, no toolspec JSON")
        if server not in SERVER_TOOLS:
            mismatches.append(f"{server}: toolspec JSON has no registry entry")
        for tool in sorted(set(ours) - set(theirs)):
            mismatches.append(f"{server}.{tool}: in registry, missing from toolspec JSON")
        for tool in sorted(set(theirs) - set(ours)):
            mismatches.append(f"{server}.{tool}: in toolspec JSON, missing from registry")
        for tool in sorted(set(ours) & set(theirs)):
            annotations = theirs[tool].get("annotations") or {}
            for attribute, hint in HINTS:
                if getattr(ours[tool], attribute) != annotations.get(hint):
                    mismatches.append(f"{server}.{tool}: {hint} registry={getattr(ours[tool], attribute)} "
                                      f"toolspec={annotations.get(hint)}")
    print("\n".join(["registry <-> toolspec mismatches:", *mismatches]) if mismatches else "registry == toolspecs")
    assert not mismatches, "\n".join(mismatches)


async def test_live_input_schemas_cover_toolspec_arguments(services: ServerServices) -> None:
    mismatches: list[str] = []
    for server, tools in _toolspecs().items():
        live = await _live_tools(services, server)
        for name, spec in tools.items():
            schema = spec.get("inputSchema") or {}
            properties = set((live[name].input_schema or {}).get("properties", {}))
            required = set((live[name].input_schema or {}).get("required", []))
            for argument in sorted(set(schema.get("properties", {})) - properties):
                mismatches.append(f"{server}.{name}: toolspec argument {argument!r} not accepted by the server")
            for argument in sorted(set(schema.get("required", [])) - required):
                mismatches.append(f"{server}.{name}: toolspec requires {argument!r}, server does not")
    print("\n".join(mismatches) or "input schemas compatible")
    assert not mismatches, "\n".join(mismatches)


# ----------------------------------------------------------------------------------------- annotations
def test_registry_classifies_servers_truthfully() -> None:
    assert READ_SERVERS | WRITE_SERVERS | INTERNAL_SERVERS == set(SERVER_TOOLS)
    assert not READ_SERVERS & WRITE_SERVERS and not (READ_SERVERS | WRITE_SERVERS) & INTERNAL_SERVERS
    for server in READ_SERVERS:
        assert all(spec.read_only and not spec.destructive for spec in SERVER_TOOLS[server]), server
    for server in WRITE_SERVERS:
        assert all(not spec.read_only for spec in SERVER_TOOLS[server]), server
    assert all(spec.read_only for spec in SERVER_TOOLS["mcp-verifier"])  # no external side effects


def test_every_catalog_write_tool_is_token_gated_on_its_server(settings: Settings) -> None:
    catalog = {(spec.server, spec.tool) for spec in ActionCatalog.from_settings(settings).specs()}
    assert catalog == set(TOKEN_GATED_TOOLS)
    for server, tool in catalog:
        spec = next((s for s in SERVER_TOOLS[server] if s.name == tool), None)
        assert spec is not None, f"{server}.{tool} missing from the registry"
        assert not spec.read_only, f"{server}.{tool} must not be annotated read-only"


async def test_built_servers_expose_exactly_the_registry(services: ServerServices) -> None:
    for server, specs in SERVER_TOOLS.items():
        live = await _live_tools(services, server)
        assert sorted(live) == sorted(spec.name for spec in specs), server
        for spec in specs:
            annotations = live[spec.name].annotations
            assert annotations is not None, f"{server}.{spec.name} has no annotations"
            observed = ToolSpec(name=spec.name, read_only=bool(annotations.read_only_hint),
                                destructive=bool(annotations.destructive_hint),
                                idempotent=bool(annotations.idempotent_hint),
                                open_world=bool(annotations.open_world_hint), description=live[spec.name].description
                                or "")
            assert observed == spec, f"{server}.{spec.name}: {observed} != {spec}"


def test_build_server_refuses_unknown_names_and_missing_dependencies(settings: Settings) -> None:
    with pytest.raises(ValueError, match="unknown MCP server"):
        build_server("mcp-nope", ServerServices(settings=settings))
    with pytest.raises(ValueError, match="ado"):
        build_server("mcp-ado", ServerServices(settings=settings))


# ---------------------------------------------------------------------------------------- write tools
@pytest.mark.parametrize(("server", "tool"), sorted(TOKEN_GATED_TOOLS))
async def test_write_tools_refuse_calls_without_a_valid_approval_token(services: ServerServices, server: str,
                                                                       tool: str) -> None:
    payload = {"work_item_type": "Requirement", "fields": {"System.Title": "x"}}
    async with Client(build_server(server, services)) as client:
        missing = await client.call_tool(tool, {"payload": payload, "action_id": "act_1"})
        empty = await client.call_tool(tool, {"payload": payload, "action_id": "act_1", "approval_token": ""})
        forged = await client.call_tool(tool, {"payload": payload, "action_id": "act_1",
                                              "approval_token": "eyJhbGciOiJub25lIn0.e30.", "validate_only": True})
    assert missing.is_error and empty.is_error and forged.is_error
    assert "unauthorized" in empty.content[0].text and "missing_token" in empty.content[0].text  # type: ignore[union-attr]
    assert "unauthorized" in forged.content[0].text  # type: ignore[union-attr]
    backend = services.write_backends.get(server)
    assert backend is not None and backend.calls == [], "nothing may reach the vendor without a token"


async def test_write_tool_positive_control_through_mcp(settings: Settings) -> None:
    hitl = make_hitl()
    action = hitl.propose_ado()
    hitl.approvals.decide(action_id=action.action_id, decision=Decision.APPROVE, assertion=hitl.identities.issue(PM))
    issued = hitl.approvals.consume_token(action_id=action.action_id, consumer=EXECUTOR_ID)
    services = ServerServices(settings=settings, write_servers={"mcp-ado-write": hitl.ado_server})
    arguments = {"payload": action.payload, "action_id": action.action_id, "approval_token": issued.token}
    async with Client(build_server("mcp-ado-write", services)) as client:
        preflight = await client.call_tool("create_work_item", {**arguments, "validate_only": True})
        created = await client.call_tool("create_work_item", arguments)
        replay = await client.call_tool("create_work_item", arguments)
        tampered = await client.call_tool("create_work_item", {**arguments, "payload": {**action.payload,
                                                                                        "work_item_type": "Bug"}})
    assert not preflight.is_error and preflight.structured_content == {"status": "valid"}
    assert not created.is_error and created.structured_content and created.structured_content["external_id"]
    assert replay.is_error and "replayed" in replay.content[0].text  # type: ignore[union-attr]
    assert tampered.is_error and "payload_mismatch" in tampered.content[0].text  # type: ignore[union-attr]
    assert len(hitl.backend.creates()) == 1 and hitl.backend.creates()[0]["payload"] == action.payload


# ----------------------------------------------------------------------------------------- read tools
async def test_read_tools_return_redacted_signals_with_cursors(services: ServerServices) -> None:
    async with Client(build_server("mcp-pagerduty", services)) as client:
        page = await client.call_tool("list_incidents", {"page_size": 2})
        notes = await client.call_tool("list_notes", {"incident_id": "90000123"})
        bad = await client.call_tool("list_incidents", {"cursor": "not-a-cursor"})
    assert not page.is_error and page.structured_content is not None
    assert [s["source_record_id"] for s in page.structured_content["signals"]] == ["90000123", "90000124"]
    assert page.structured_content["next_cursor"]
    assert notes.structured_content is not None and "hunter2" not in json.dumps(notes.structured_content)
    assert bad.is_error and "validation_failed" in bad.content[0].text  # type: ignore[union-attr]
    async with Client(build_server("mcp-ado", services)) as client:
        changed = await client.call_tool("list_changed_work_items", {})
        items = await client.call_tool("get_work_items", {"ids": [9100123, "404"]})
        comments = await client.call_tool("list_comments", {"work_item_id": "9100123"})
    assert changed.structured_content is not None
    assert [(i["id"], i["rev"]) for i in changed.structured_content["items"]] == [("9100123", "7"), ("9100456", "3")]
    assert items.structured_content is not None and items.structured_content["missing"] == ["404"]
    assert comments.structured_content is not None and len(comments.structured_content["signals"]) == 1


async def test_placeholder_backed_tools_fail_with_typed_unavailable(settings: Settings,
                                                                    services: ServerServices) -> None:
    assert services.redactor is not None
    real = ServerServices(settings=settings, redactor=services.redactor,
                          ado=HttpAdoConnector.from_settings(settings, services.redactor))
    async with Client(build_server("mcp-ado", real)) as client:
        result = await client.call_tool("list_changed_work_items", {})
    assert result.is_error
    assert "unavailable" in result.content[0].text and "Q-04" in result.content[0].text  # type: ignore[union-attr]


async def test_figma_msgraph_and_kb_read_tools(services: ServerServices) -> None:
    async with Client(build_server("mcp-figma", services)) as client:
        found = await client.call_tool("search_dls", {"query": "evidence card"})
        images = await client.call_tool("render_images", {"file_key": "DLSdemoLibraryKey01", "ids": ["10:1"]})
    assert found.structured_content is not None and found.structured_content["components"][0]["name"] == \
        "Card/Evidence"
    assert images.structured_content is not None and "10:1" in images.structured_content["images"]
    async with Client(build_server("mcp-msgraph", services)) as client:
        rows = await client.call_tool("read_workbook", {"item_id": "01DEMOTRACKER0001"})
        delta = await client.call_tool("mail_delta", {"page_size": 10})
    assert rows.structured_content is not None and len(rows.structured_content["signals"]) == 2
    assert delta.structured_content is not None and len(delta.structured_content["signals"]) == 4
    async with Client(build_server("mcp-kb", services)) as client:
        hits = await client.call_tool("search_articles", {"query": "standardized log mapping file"})
    assert hits.structured_content is not None and hits.structured_content["hits"][0]["article_id"] == "KB0001001"


# ------------------------------------------------------------------------------------ internal servers
async def test_store_verifier_and_correlation_servers(services: ServerServices, settings: Settings) -> None:
    assert services.redactor is not None
    signals = [s.model_dump(mode="json") for c in golden_connectors(services.redactor, settings=settings).values()
               for s in c.poll(None).signals]
    async with Client(build_server("mcp-store", services)) as client:
        first = await client.call_tool("put_signals", {"signals": signals})
        again = await client.call_tool("put_signals", {"signals": signals[:3]})
        search = await client.call_tool("evidence_search", {"query": "TVDHD mapping file", "top_k": 3})
        counts = await client.call_tool("run_named_query", {"name": "signal_counts_by_source"})
        unknown = await client.call_tool("run_named_query", {"name": "drop_everything"})
        raw = await client.call_tool("put_signals", {"signals": [{**signals[0], "signal_id": "sig_x",
                                                                  "provenance_hash": None,
                                                                  "raw_text": "Password=hunter2"}]})
    assert first.structured_content is not None and len(first.structured_content["stored"]) == 21
    assert again.structured_content is not None and again.structured_content["stored"] == []
    assert search.structured_content is not None and search.structured_content["hits"][0]["permalink"]
    assert counts.structured_content is not None and {r["source_system"] for r in counts.structured_content["rows"]} \
        == {"ado", "servicenow", "pagerduty", "email", "onedrive"}
    assert unknown.is_error and "not_found" in unknown.content[0].text  # type: ignore[union-attr]
    assert raw.structured_content is not None and raw.structured_content["rejected"] == ["sig_x"]  # DM-5
    async with Client(build_server("mcp-verifier", services)) as client:
        redacted = await client.call_tool("redact", {"text": "Password=hunter2 mail ops@example.com"})
        policy = await client.call_tool("policy_evaluate", {"action_type": "OUTREACH_SEND_INTERNAL",
                                                            "payload": {"to": ["someone@example.com"]}})
    assert redacted.structured_content is not None and "hunter2" not in redacted.structured_content["text"]
    assert policy.structured_content is not None and policy.structured_content["allowed"] is False
    async with Client(build_server("mcp-correlation", services)) as client:
        case_note = next(s for s in signals if s["source_record_id"] == "1b2c3d4e5f6071829304a5b6c7d8e9f0")
        related = await client.call_tool("related", {"signal_id": case_note["signal_id"]})
    assert related.structured_content is not None
    assert related.structured_content["parent"] == case_note["parent_signal_id"]
    assert len(related.structured_content["cross_referenced"]) >= 2  # PD incident + ADO bug


async def test_ledger_server_runs_the_hitl_protocol(settings: Settings) -> None:
    hitl = make_hitl()
    draft = grounded_draft(hitl.verifier)
    payload = ado_payload(draft)
    ref = hitl.subjects.put("requirement_draft", draft)
    report = hitl.verify(draft, "ADO_CREATE_WORK_ITEM", payload)
    ledger_writes = WriteToolServer("mcp-ledger", hitl.guard, ledger_write_handlers())
    services = ServerServices(settings=settings, approvals=hitl.approvals, ledger=hitl.ledger,
                              write_servers={"mcp-ledger": ledger_writes})
    async with Client(build_server("mcp-ledger", services)) as client:
        proposed = await client.call_tool("propose", {
            "action_type": "ADO_CREATE_WORK_ITEM", "target": {"system": "ado"}, "payload": payload, "draft_ref": ref,
            "report": report.model_dump(mode="json"), "proposed_by": "spiffe://agents.example/pm-agent-core"})
        assert proposed.structured_content is not None, proposed.content
        action_id = proposed.structured_content["action_id"]
        decided = await client.call_tool("decide", {"action_id": action_id, "decision": "approve",
                                                    "assertion": hitl.identities.issue(PM)})
        forged = await client.call_tool("decide", {"action_id": action_id, "decision": "approve",
                                                   "assertion": "not-a-jwt"})
        consumed = await client.call_tool("consume_token", {"action_id": action_id, "consumer": EXECUTOR_ID})
        again = await client.call_tool("consume_token", {"action_id": action_id, "consumer": EXECUTOR_ID})
        entries = await client.call_tool("query", {"action_id": action_id})
    assert decided.structured_content is not None and decided.structured_content["token_issued"] is True
    assert "token" not in json.dumps(decided.structured_content).replace("token_issued", "")  # never via decide
    assert forged.is_error and "unauthenticated" in forged.content[0].text  # type: ignore[union-attr]
    assert consumed.structured_content is not None and consumed.structured_content["token"].count(".") == 2
    assert again.is_error and "token_consumed" in again.content[0].text  # type: ignore[union-attr]
    assert entries.structured_content is not None
    types = [e["entry_type"] for e in entries.structured_content["entries"]]
    assert types[:3] == [LedgerEntryType.ACTION_PROPOSED.value, LedgerEntryType.DECISION_RECORDED.value,
                         LedgerEntryType.TOKEN_ISSUED.value]
    assert types.count(LedgerEntryType.TOKEN_CONSUMED.value) == 1
    assert types.count(LedgerEntryType.POLICY_DENIED.value) == 2  # forged decision + second consumption


# -------------------------------------------------------------------------------------------- hosting
def test_http_app_serves_streamable_mcp_and_healthz(services: ServerServices) -> None:
    app = http_app(build_server("mcp-kb", services))
    headers = {"accept": "application/json, text/event-stream", "content-type": "application/json",
               "mcp-protocol-version": "2025-06-18"}
    with TestClient(app) as client:
        assert client.get("/healthz").json() == {"status": "ok", "server": "mcp-kb"}
        init = client.post("/mcp", headers=headers, json={
            "jsonrpc": "2.0", "id": 0, "method": "initialize",
            "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}}})
        listed = client.post("/mcp", headers=headers, json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    assert init.status_code == 200
    tools = listed.json()["result"]["tools"]
    assert [t["name"] for t in tools] == ["search_articles"] and tools[0]["annotations"]["readOnlyHint"] is True
