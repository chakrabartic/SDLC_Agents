"""Connector layer (docs/09 §2–§7): normalizers, fixture connectors, request builders and the golden set.

No network: HTTP connectors are exercised through their pure request builders, a recorded-response transport and
``httpx.MockTransport``; their default transport fails loudly with the open question that blocks it (Q-xx).
"""
from __future__ import annotations

import json
from collections import Counter
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest

from pm_agent.connectors import ado, figma, kb, msgraph, pagerduty, servicenow
from pm_agent.connectors.base import ConnectorError, ConnectorStatus, deterministic_signal_id
from pm_agent.connectors.common import (
    IngestContext,
    decode_offset_cursor,
    encode_offset_cursor,
    html_to_text,
    parse_utc,
    person_ref,
)
from pm_agent.connectors.entities import EntityResolver
from pm_agent.connectors.fixtures import (
    FIXTURE_ROOT,
    fixture_figma_connector,
    fixture_kb_adapter,
    golden_connectors,
    load_golden_manifest,
)
from pm_agent.connectors.http import HttpRequest, HttpxTransport, UnconfiguredTransport, error_for_status
from pm_agent.grounding import LocalRedactor
from pm_agent.grounding.chunking import chunk_signal
from pm_agent.grounding.dedup import jaccard_exact, jaccard_minhash
from pm_agent.placeholders import PlaceholderNotImplemented
from pm_agent.schemas import RecordType, Signal, SignalTypeHint, SourceSystem
from pm_agent.settings import Settings, load_settings

SECRET_MARKERS = ("hunter2", "3f9a1c0e7b2d4e6f8a0b1c2d3e4f5a6b")
ORG, PROJECT = "example-org", "ExampleProject"
SNOW = "https://example.service-now.com"


# ----------------------------------------------------------------------------------------------- fixtures
@pytest.fixture(name="settings", scope="module")
def _settings() -> Settings:
    return load_settings(environ={"PM_AGENT_ENV": "local"})


@pytest.fixture(name="redactor", scope="module")
def _redactor(settings: Settings) -> LocalRedactor:
    return LocalRedactor.from_settings(settings, pseudonym_key=b"test-key")


@pytest.fixture(name="entities", scope="module")
def _entities(settings: Settings) -> EntityResolver:
    return EntityResolver.from_settings(settings)


def _load(*parts: str) -> dict[str, Any]:
    data: dict[str, Any] = json.loads(FIXTURE_ROOT.joinpath(*parts).read_text(encoding="utf-8"))
    return data


def _ado_items() -> list[dict[str, Any]]:
    return list(_load("ado", "workitems.json")["workitemsbatch"]["value"])


def _snow() -> dict[str, Any]:
    return _load("servicenow", "tables.json")


def _pd() -> dict[str, Any]:
    return _load("pagerduty", "incidents.json")


def _mail() -> list[dict[str, Any]]:
    return list(_load("msgraph", "mail.json")["delta"]["value"])


def _drive() -> dict[str, Any]:
    return _load("msgraph", "drive.json")


def _no_secret(signal: Signal) -> None:
    blob = f"{signal.title or ''}\n{signal.raw_text}"
    for marker in SECRET_MARKERS:
        assert marker not in blob, (signal.source_record_id, marker)


# ------------------------------------------------------------------------------------------ common helpers
def test_html_to_text_keeps_block_breaks_and_unescapes() -> None:
    text = html_to_text("<div>Step&nbsp;1 &amp; 2</div><div>Line <b>two</b><br>three</div><ul><li>a</li><li>b</li></ul>")
    assert "<" not in text and "&amp;" not in text
    assert text.splitlines()[0] == "Step 1 & 2"
    assert "Line two" in text and "three" in text and "a" in text.splitlines() and "b" in text.splitlines()
    assert html_to_text("<style>p{color:red}</style><script>alert(1)</script><p>kept</p>") == "kept"
    assert html_to_text("plain text stays") == "plain text stays"


def test_parse_utc_accepts_vendor_formats() -> None:
    assert parse_utc("2026-05-09T10:02:00Z") == datetime(2026, 5, 9, 10, 2, tzinfo=UTC)
    assert parse_utc("2026-05-09T12:02:00+02:00") == datetime(2026, 5, 9, 10, 2, tzinfo=UTC)
    assert parse_utc("2026-05-09T10:02:00.1234567Z") == datetime(2026, 5, 9, 10, 2, 0, 123456, tzinfo=UTC)
    assert parse_utc("2026-05-09 10:02:00") == datetime(2026, 5, 9, 10, 2, tzinfo=UTC)  # ServiceNow (UTC)
    assert parse_utc(None) is None and parse_utc("not a date") is None


def test_offset_cursor_round_trip_and_rejects_tampering() -> None:
    assert decode_offset_cursor(None) == 0
    assert decode_offset_cursor(encode_offset_cursor(42)) == 42
    for bad in ("garbage", "fx:-1", "fx:abc"):
        with pytest.raises(ConnectorError) as excinfo:
            decode_offset_cursor(bad)
        assert excinfo.value.code == "validation_failed"


def test_person_ref_is_pseudonymous_and_deterministic(redactor: LocalRedactor) -> None:
    first = person_ref(redactor, "l2.support@eng.example.com", role_hint="L2 Support")
    again = person_ref(redactor, "L2.Support@eng.example.com ")
    assert first is not None and again is not None
    assert first.token == again.token and first.token.startswith("PERSON_")
    assert "l2.support" not in first.token and first.role_hint == "L2 Support"
    assert person_ref(redactor, None) is None and person_ref(redactor, "  ") is None


# --------------------------------------------------------------------------------------------- entities
def test_entity_resolver_uses_config_gazetteer(entities: EntityResolver) -> None:
    assert entities.customer("Customer: CNE.") is not None
    assert entities.customer("Reported by Contoso Energy").name == "Contoso Energy"  # type: ignore[union-attr]
    assert entities.customer("Fabrikam Petroleum and Northwind Offshore asked") is None  # ambiguous
    assert entities.customers_in("Fabrikam Petroleum and NWO") == ["Fabrikam Petroleum", "Northwind Offshore"]
    assert entities.customer("contact drilling.lead@contoso-energy.example.com") is None  # addresses don't count
    assert entities.canonical_customer("contoso energy") == "Contoso Energy"
    assert entities.canonical_customer("Unknown Corp") is None
    assert entities.product("the DrillOps real-time dashboard") == "DrillOps"
    assert entities.product("ExampleProject\\Lumi for Drilling\\Data Delivery") == "Lumi for Drilling"
    assert entities.product("nothing relevant") is None


# --------------------------------------------------------------------------------------------------- ADO
def test_ado_permalink_format() -> None:
    assert ado.ado_permalink(ORG, PROJECT, 9100123) == \
        "https://dev.azure.com/example-org/ExampleProject/_workitems/edit/9100123"
    assert ado.ado_permalink("org", "My Project", "7") == "https://dev.azure.com/org/My%20Project/_workitems/edit/7"


def test_normalize_ado_work_item(redactor: LocalRedactor, entities: EntityResolver) -> None:
    raw = _ado_items()[0]
    signal = ado.normalize_ado_work_item(raw, redactor=redactor, organization=ORG, project=PROJECT, entities=entities)
    assert signal.source_system is SourceSystem.ADO and signal.record_type is RecordType.WORK_ITEM
    assert (signal.source_record_id, signal.source_version) == ("9100123", "7")
    assert signal.signal_id == deterministic_signal_id(SourceSystem.ADO, "9100123", "7")
    assert signal.permalink == "https://dev.azure.com/example-org/ExampleProject/_workitems/edit/9100123"
    assert signal.title and signal.title.startswith("Standardized log channel not populated")
    assert "<div>" not in signal.raw_text and "Upload the standardization mapping file" in signal.raw_text
    assert "Acceptance criteria" not in signal.raw_text  # P1 readiness gap: none stated
    refs = {(r.system, r.record_id) for r in signal.cross_refs}
    assert {(SourceSystem.SERVICENOW, "CS9000123"), (SourceSystem.PAGERDUTY, "90000123")} <= refs
    assert signal.customer.name == "Contoso Energy" and signal.product == "DrillOps"
    assert signal.signal_type_hint is SignalTypeHint.DEFECT
    assert signal.captured_utc == datetime(2026, 5, 11, 9, 30, tzinfo=UTC)
    assert signal.author is not None and "eng.lead" not in signal.author.token
    assert signal.ingestion is not None and signal.ingestion.connector == "mcp-ado"


def test_normalize_ado_feature_keeps_acceptance_criteria(redactor: LocalRedactor, entities: EntityResolver) -> None:
    raw = _ado_items()[1]
    signal = ado.normalize_ado_work_item(raw, redactor=redactor, organization=ORG, project=PROJECT, entities=entities,
                                         ctx=IngestContext(connector="fixture-ado", run_id="run-1"))
    assert signal.signal_type_hint is SignalTypeHint.FEATURE_REQUEST
    assert "Acceptance criteria" in signal.raw_text and "Export all depth logs" in signal.raw_text
    assert signal.customer.name is None  # two customers named ⇒ ambiguous, never guessed
    assert signal.product == "Lumi for Drilling"
    assert signal.ingestion is not None and signal.ingestion.run_id == "run-1"


def test_normalize_ado_comment_is_redacted_child(redactor: LocalRedactor, entities: EntityResolver) -> None:
    parent = ado.normalize_ado_work_item(_ado_items()[0], redactor=redactor, organization=ORG, project=PROJECT,
                                         entities=entities)
    raw = _load("ado", "workitems.json")["comments"]["9100123"]["comments"][0]
    comment = ado.normalize_ado_comment(raw, redactor=redactor, parent=parent)
    assert comment.record_type is RecordType.COMMENT and comment.parent_signal_id == parent.signal_id
    assert (comment.source_record_id, comment.source_version) == ("9100123:5011", "1")
    assert comment.permalink.startswith(parent.permalink)
    _no_secret(comment)
    assert comment.redaction is not None and comment.redaction.findings.get("AUTH_TOKEN", 0) >= 1
    assert comment.customer == parent.customer and comment.product == parent.product
    again = ado.normalize_ado_comment(raw, redactor=redactor, parent=parent)
    assert (again.signal_id, again.provenance_hash) == (comment.signal_id, comment.provenance_hash)


def test_ado_wiql_and_batch_request_builders() -> None:
    api = ado.AdoApi(ORG, PROJECT)
    text = ado.AdoApi.wiql_changed_text("2026-05-09T00:00:00Z", area_paths=["ExampleProject\\Bob's Area"])
    assert "[System.TeamProject] = @project" in text
    assert "[System.ChangedDate] >= '2026-05-09T00:00:00Z'" in text
    assert "UNDER 'ExampleProject\\Bob''s Area'" in text  # quotes escaped, never interpolated raw
    assert text.rstrip().endswith("ORDER BY [System.ChangedDate] ASC")
    request = api.wiql_changed_since(None, top=50)
    assert request.method == "POST"
    assert request.url == "https://dev.azure.com/example-org/ExampleProject/_apis/wit/wiql"
    assert dict(request.params) == {"api-version": "7.1", "timePrecision": "true", "$top": "50"}
    assert "[System.ChangedDate] >=" not in request.json_body["query"]  # first poll: full window
    batch = api.workitems_batch([9100123, 9100456])
    assert batch.url.endswith("/_apis/wit/workitemsbatch") and dict(batch.params) == {"api-version": "7.1"}
    assert batch.json_body == {"ids": [9100123, 9100456], "$expand": "Relations", "errorPolicy": "Omit"}
    with pytest.raises(ValueError):
        api.workitems_batch(list(range(201)))
    comments = api.comments(9100123)
    assert comments.method == "GET"
    assert comments.url.endswith("/_apis/wit/workItems/9100123/comments")
    assert dict(comments.params)["api-version"] == "7.1-preview.4"


def test_ado_create_request_is_json_patch_with_validate_only() -> None:
    api = ado.AdoApi(ORG, PROJECT)
    request = api.create_work_item("User Story", {"System.Title": "Bulk export", "System.Tags": "pm"},
                                   validate_only=True, idempotency_tag="pm-agent:act_1")
    assert request.method == "POST"
    assert request.url == "https://dev.azure.com/example-org/ExampleProject/_apis/wit/workitems/$User%20Story"
    params = dict(request.params)
    assert params == {"api-version": "7.1", "validateOnly": "true"} and "bypassRules" not in params
    assert request.headers["Content-Type"] == "application/json-patch+json"
    ops = {op["path"]: op for op in request.json_body}
    assert ops["/fields/System.Title"] == {"op": "add", "path": "/fields/System.Title", "value": "Bulk export"}
    assert ops["/fields/System.Tags"]["value"] == "pm; pm-agent:act_1"
    real = api.create_work_item("Requirement", {"System.Title": "x"}, validate_only=False)
    assert "validateOnly" not in dict(real.params)
    assert {"op": "add", "path": "/fields/System.Tags", "value": "pm-agent:none"} not in real.json_body


def test_ado_add_relations_request_guards_revision() -> None:
    request = ado.AdoApi(ORG, PROJECT).add_relations(
        9100123, rev=7, relations=[{"rel": "System.LinkTypes.Related", "url": "https://dev.azure.com/x/y/_apis/wit/"
                                                                             "workItems/9100456"}],
        validate_only=True)
    assert request.method == "PATCH" and request.url.endswith("/_apis/wit/workitems/9100123")
    assert request.json_body[0] == {"op": "test", "path": "/rev", "value": 7}
    assert request.json_body[1]["op"] == "add" and request.json_body[1]["path"] == "/relations/-"
    assert dict(request.params)["validateOnly"] == "true"
    with pytest.raises(ValueError):
        ado.AdoApi(ORG, PROJECT).add_relations(1, rev=1, relations=[{"rel": "System.LinkTypes.Hierarchy-Forward",
                                                                     "url": "u"}], validate_only=False)


def test_fixture_ado_connector(redactor: LocalRedactor, entities: EntityResolver) -> None:
    connector = ado.FixtureAdoConnector(redactor, entities=entities)
    result = connector.poll(None)
    assert result.status is ConnectorStatus.OK and result.source is SourceSystem.ADO
    ids = [s.source_record_id for s in result.signals]
    assert ids == ["9100123", "9100123:5011", "9100456"]  # parent then its children, delivery order
    assert connector.get("9100123") == result.signals[0]
    assert [s.source_record_id for s in connector.comments("9100123")] == ["9100123:5011"]
    assert connector.get("404") is None
    assert [s.source_record_id for s in connector.search("bulk export depth logs")][0] == "9100456"
    records = connector.poll_records(None, limit=1)
    assert [s.source_record_id for s in records.signals] == ["9100123"] and records.next_cursor
    assert [s.source_record_id for s in connector.poll_records(records.next_cursor, limit=1).signals] == ["9100456"]


def test_http_ado_connector_fails_loudly_until_q04(settings: Settings, redactor: LocalRedactor) -> None:
    connector = ado.HttpAdoConnector.from_settings(settings, redactor)
    with pytest.raises(PlaceholderNotImplemented) as excinfo:
        connector.poll(None)
    assert excinfo.value.question_id == "Q-04"


class RecordedTransport:
    """Replays recorded vendor responses keyed by (method, url-suffix); records every request it was given."""

    def __init__(self, responses: Mapping[tuple[str, str], Any]) -> None:
        self.responses = dict(responses)
        self.requests: list[HttpRequest] = []

    def send(self, request: HttpRequest) -> Any:
        self.requests.append(request)
        for (method, suffix), body in self.responses.items():
            if request.method == method and request.url.endswith(suffix):
                return body(request) if callable(body) else body
        raise AssertionError(f"unexpected request {request.method} {request.url}")


def test_http_ado_connector_with_recorded_responses(redactor: LocalRedactor, entities: EntityResolver) -> None:
    fixture = _load("ado", "workitems.json")
    wiql = {"queryType": "flat", "workItems": [{"id": 9100123}, {"id": 9100456}]}
    transport = RecordedTransport({
        ("POST", "/_apis/wit/wiql"): wiql,
        ("POST", "/_apis/wit/workitemsbatch"): fixture["workitemsbatch"],
        ("GET", "/workItems/9100123/comments"): fixture["comments"]["9100123"],
        ("GET", "/workItems/9100456/comments"): fixture["comments"]["9100456"],
    })
    connector = ado.HttpAdoConnector(organization=ORG, project=PROJECT, redactor=redactor, entities=entities,
                                     transport=transport)
    result = connector.poll("2026-05-01T00:00:00Z", limit=10)
    expected = ado.FixtureAdoConnector(redactor, entities=entities).poll(None)
    assert [s.signal_id for s in result.signals] == [s.signal_id for s in expected.signals]
    assert result.next_cursor == "2026-05-14T16:20:00Z"  # newest System.ChangedDate seen
    assert "[System.ChangedDate] >= '2026-05-01T00:00:00Z'" in transport.requests[0].json_body["query"]


# -------------------------------------------------------------------------------------------- ServiceNow
def test_servicenow_permalink_format() -> None:
    assert servicenow.servicenow_permalink(SNOW + "/", "incident", "6f1c2b3a4d5e6f708192a3b4c5d6e7f8") == \
        "https://example.service-now.com/incident.do?sys_id=6f1c2b3a4d5e6f708192a3b4c5d6e7f8"


def test_normalize_servicenow_case_redacts_pii(redactor: LocalRedactor, entities: EntityResolver) -> None:
    raw = _snow()["tables"]["sn_customerservice_case"]["result"][0]
    signal = servicenow.normalize_servicenow_record(raw, redactor=redactor, instance_url=SNOW, entities=entities)
    assert signal.source_system is SourceSystem.SERVICENOW and signal.record_type is RecordType.CASE
    assert (signal.source_record_id, signal.source_version) == ("CS9000123", "6")
    assert signal.permalink == f"{SNOW}/sn_customerservice_case.do?sys_id=0a1b2c3d4e5f60718293a4b5c6d7e8f9"
    assert "drilling.lead@contoso-energy.example.com" not in signal.raw_text and "555 010 4477" not in signal.raw_text
    assert signal.redaction is not None
    assert signal.redaction.findings.get("EMAIL_ADDRESS") == 1 and signal.redaction.findings.get("PHONE_NUMBER") == 1
    assert signal.customer.name == "Contoso Energy" and signal.product == "DrillOps"
    assert signal.captured_utc == datetime(2026, 5, 10, 9, 0, tzinfo=UTC)
    incident = servicenow.normalize_servicenow_record(_snow()["tables"]["incident"]["result"][0], redactor=redactor,
                                                      instance_url=SNOW, entities=entities)
    assert incident.record_type is RecordType.INCIDENT and incident.source_record_id == "INC9000456"


def test_normalize_servicenow_journal_is_child_with_cross_refs(redactor: LocalRedactor,
                                                               entities: EntityResolver) -> None:
    parent = servicenow.normalize_servicenow_record(_snow()["tables"]["sn_customerservice_case"]["result"][0],
                                                    redactor=redactor, instance_url=SNOW, entities=entities)
    raw = _snow()["sys_journal_field"]["result"][0]
    note = servicenow.normalize_servicenow_journal(raw, redactor=redactor, parent=parent)
    assert note.record_type is RecordType.JOURNAL_NOTE and note.parent_signal_id == parent.signal_id
    assert note.source_record_id == "1b2c3d4e5f6071829304a5b6c7d8e9f0" and note.source_version == "1"
    refs = {(r.system, r.record_id) for r in note.cross_refs}
    assert {(SourceSystem.PAGERDUTY, "90000123"), (SourceSystem.ADO, "9100123")} <= refs
    assert note.author is not None and "l2.support" not in note.author.token
    assert note.permalink.startswith(parent.permalink)


def test_servicenow_request_builders() -> None:
    api = servicenow.ServiceNowApi(SNOW)
    request = api.table_delta("incident", "2026-05-09 10:00:00", limit=50, offset=100)
    assert request.method == "GET" and request.url == f"{SNOW}/api/now/table/incident"
    params = dict(request.params)
    assert params["sysparm_query"] == "sys_updated_on>=2026-05-09 10:00:00^ORDERBYsys_updated_on"
    assert params["sysparm_display_value"] == "all"
    assert (params["sysparm_limit"], params["sysparm_offset"]) == ("50", "100")
    first = dict(api.table_delta("incident", None, limit=10).params)
    assert first["sysparm_query"] == "ORDERBYsys_updated_on"
    journal = dict(api.journal("0a1b2c3d4e5f60718293a4b5c6d7e8f9").params)
    assert journal["sysparm_query"] == \
        "element_id=0a1b2c3d4e5f60718293a4b5c6d7e8f9^elementINcomments,work_notes^ORDERBYsys_created_on"
    with pytest.raises(ValueError):
        api.journal("x^ORDERBYsys_id")  # sys_id must be 32 hex: no encoded-query injection
    with pytest.raises(ValueError):
        api.table_delta("incident^x", None, limit=1)


def test_fixture_servicenow_connector(redactor: LocalRedactor, entities: EntityResolver) -> None:
    connector = servicenow.FixtureServiceNowConnector(redactor, entities=entities)
    result = connector.poll(None)
    assert [s.source_record_id for s in result.signals] == [
        "INC9000456", "CS9000123", "1b2c3d4e5f6071829304a5b6c7d8e9f0", "2c3d4e5f60718293a4b5c6d7e8f90a1b"]
    assert connector.get("CS9000123") == connector.get("0a1b2c3d4e5f60718293a4b5c6d7e8f9")  # number or sys_id
    assert len(connector.journal("CS9000123")) == 2


def test_http_servicenow_connector_fails_loudly_until_q05(settings: Settings, redactor: LocalRedactor) -> None:
    with pytest.raises(PlaceholderNotImplemented) as excinfo:
        servicenow.HttpServiceNowConnector.from_settings(settings, redactor).poll(None)
    assert excinfo.value.question_id == "Q-05"


# --------------------------------------------------------------------------------------------- PagerDuty
def test_normalize_pagerduty_incident(redactor: LocalRedactor, entities: EntityResolver) -> None:
    raw = _pd()["incidents"]["incidents"][0]
    signal = pagerduty.normalize_pagerduty_incident(raw, redactor=redactor, entities=entities)
    assert signal.source_system is SourceSystem.PAGERDUTY and signal.record_type is RecordType.INCIDENT
    assert (signal.source_record_id, signal.source_version) == ("90000123", "2026-05-09T12:40:00Z")
    assert signal.permalink == "https://example.pagerduty.com/incidents/Q1GOLDENP1PD01"
    assert signal.raw_text.startswith("For well WELL-0042(h), the channel TVDHD")
    assert signal.customer.name == "Contoso Energy"
    assert pagerduty.pagerduty_permalink("https://example.pagerduty.com/", "Q1") == \
        "https://example.pagerduty.com/incidents/Q1"


def test_normalize_pagerduty_note_and_log_entry(redactor: LocalRedactor, entities: EntityResolver) -> None:
    parent = pagerduty.normalize_pagerduty_incident(_pd()["incidents"]["incidents"][0], redactor=redactor,
                                                    entities=entities)
    note = pagerduty.normalize_pagerduty_note(_pd()["notes"]["Q1GOLDENP1PD01"]["notes"][0], redactor=redactor,
                                              parent=parent)
    assert note.record_type is RecordType.JOURNAL_NOTE and note.parent_signal_id == parent.signal_id
    assert note.source_record_id == "PNOTE0P1A"
    _no_secret(note)
    assert "oncall.lead@ops.example.com" not in note.raw_text
    assert note.redaction is not None and note.redaction.findings.get("CONNECTION_STRING_PASSWORD") == 1
    entry = pagerduty.normalize_pagerduty_log_entry(
        _pd()["log_entries"]["Q1GOLDENP1PD01"]["log_entries"][2], redactor=redactor, parent=parent)
    assert entry.record_type is RecordType.JOURNAL_NOTE and entry.parent_signal_id == parent.signal_id
    assert entry.source_record_id == "PLOG0P1RES" and "Resolved" in entry.raw_text


def test_pagerduty_request_builders() -> None:
    api = pagerduty.PagerDutyApi("https://api.pagerduty.com")
    request = api.incidents("2026-05-09T00:00:00Z", statuses=["triggered", "resolved"], service_ids=["PSVC0LOGSTD"],
                            limit=500, offset=100)
    assert request.method == "GET" and request.url == "https://api.pagerduty.com/incidents"
    assert ("statuses[]", "triggered") in request.params and ("statuses[]", "resolved") in request.params
    assert ("service_ids[]", "PSVC0LOGSTD") in request.params and ("since", "2026-05-09T00:00:00Z") in request.params
    assert ("limit", "100") in request.params  # capped at the vendor maximum
    assert request.headers["Accept"] == "application/vnd.pagerduty+json;version=2"
    assert api.notes("Q1GOLDENP1PD01").url == "https://api.pagerduty.com/incidents/Q1GOLDENP1PD01/notes"
    assert api.log_entries("Q1GOLDENP1PD01").url.endswith("/incidents/Q1GOLDENP1PD01/log_entries")
    with pytest.raises(ValueError):
        api.notes("../users")


def test_fixture_pagerduty_connector_pages_and_delivers_duplicate(redactor: LocalRedactor,
                                                                  entities: EntityResolver) -> None:
    connector = pagerduty.FixturePagerDutyConnector(redactor, entities=entities)
    first = connector.poll(None, limit=2)
    second = connector.poll(first.next_cursor, limit=2)
    assert [s.source_record_id for s in first.signals if s.record_type is RecordType.INCIDENT] == \
        ["90000123", "90000124"]
    assert [s.source_record_id for s in second.signals if s.record_type is RecordType.INCIDENT] == ["90000123"]
    assert first.signals[0].signal_id == second.signals[0].signal_id  # same record version delivered twice
    assert first.signals[0].provenance_hash == second.signals[0].provenance_hash
    done = connector.poll(second.next_cursor, limit=2)
    assert done.signals == [] and done.next_cursor == second.next_cursor  # resume point is stable
    assert connector.get("Q1GOLDENP1PD01") == connector.get("90000123")
    assert [s.source_record_id for s in connector.notes("90000123")] == ["PNOTE0P1A"]
    assert len(connector.log_entries("90000123")) == 3
    with pytest.raises(ConnectorError):
        connector.poll(None, limit=0)


def test_http_pagerduty_connector_fails_loudly_until_q07(settings: Settings, redactor: LocalRedactor) -> None:
    with pytest.raises(PlaceholderNotImplemented) as excinfo:
        pagerduty.HttpPagerDutyConnector.from_settings(settings, redactor).poll(None)
    assert excinfo.value.question_id == "Q-07"


# ------------------------------------------------------------------------------------- Microsoft Graph
def test_normalize_graph_message_prefers_unique_body(redactor: LocalRedactor, entities: EntityResolver) -> None:
    root_raw, reply_raw = _mail()[0], _mail()[1]
    root = msgraph.normalize_graph_message(root_raw, redactor=redactor, entities=entities)
    reply = msgraph.normalize_graph_message(reply_raw, redactor=redactor, entities=entities, thread_root=root)
    assert root.source_system is SourceSystem.EMAIL and root.record_type is RecordType.EMAIL_MESSAGE
    assert (root.source_record_id, root.source_version) == ("AAMkADEMO-P1-MSG-001", "CQAAABYAAADEMO-P1-MSG-001-v1")
    assert root.permalink == root_raw["webLink"] and root.parent_signal_id is None
    assert "drilling.lead@contoso-energy.example.com" not in root.raw_text and "555 010 4477" not in root.raw_text
    assert root.customer.name == "Contoso Energy"
    assert reply.parent_signal_id == root.signal_id
    assert "From:" not in reply.raw_text  # quoted history excluded (uniqueBody)
    refs = {(r.system, r.record_id) for r in reply.cross_refs}
    assert {(SourceSystem.SERVICENOW, "CS9000123"), (SourceSystem.ADO, "9100123")} <= refs
    assert reply.customer.name == "Contoso Energy"  # subject "(CNE)"
    assert reply.author is not None and "l2.support" not in reply.author.token


def test_normalize_graph_document_and_tracker_rows(redactor: LocalRedactor, entities: EntityResolver) -> None:
    drive = _drive()
    tracker, document = drive["delta"]["value"]
    doc = msgraph.normalize_graph_document(document, redactor=redactor, text=drive["item_text"]["01DEMODOC0002"],
                                           entities=entities)
    assert doc.source_system is SourceSystem.ONEDRIVE and doc.record_type is RecordType.DOCUMENT
    assert (doc.source_record_id, doc.source_version) == ("01DEMODOC0002", "{DEMO-DOC-0002},3")
    assert doc.permalink == document["webUrl"] and doc.product == "Lumi for Drilling"
    rows = msgraph.normalize_workbook_rows(tracker, drive["workbooks"]["01DEMOTRACKER0001"]["Tracker"],
                                           redactor=redactor, sheet="Tracker", entities=entities)
    assert [r.source_record_id for r in rows] == ["01DEMOTRACKER0001!Tracker!2", "01DEMOTRACKER0001!Tracker!3"]
    p1, p2 = rows
    assert p1.record_type is RecordType.TRACKER_ROW and p1.raw_text.startswith("Ref: P1\nCustomer: CNE")
    assert "pm.owner@pm.example.com" not in p1.raw_text
    assert p1.customer.name == "Contoso Energy" and p1.product == "DrillOps"
    assert p2.customer.name is None and p2.product == "Lumi for Drilling"
    assert "activeCell=" in p1.permalink and "A2%3AG2" in p1.permalink
    assert p1.sensitivity.purview_label == "Internal"
    assert p1.source_version != p2.source_version  # content-addressed row versions
    same = msgraph.normalize_workbook_rows(tracker, drive["workbooks"]["01DEMOTRACKER0001"]["Tracker"],
                                           redactor=redactor, sheet="Tracker", entities=entities)
    assert [r.signal_id for r in same] == [r.signal_id for r in rows]


def test_graph_request_builders_and_cursor_guard() -> None:
    api = msgraph.GraphApi()
    request = api.mail_delta("pm-intake@pm.example.com", "inbox", page_size=50)
    assert request.method == "GET"
    assert request.url == ("https://graph.microsoft.com/v1.0/users/pm-intake@pm.example.com/mailFolders/inbox/"
                           "messages/delta")
    assert request.headers["Prefer"].startswith("odata.maxpagesize=50")
    nxt = api.mail_delta("pm-intake@pm.example.com", "inbox", page_size=50,
                         cursor="https://graph.microsoft.com/v1.0/users/x/mailFolders/inbox/messages/delta?$skiptoken=1")
    assert nxt.url.endswith("$skiptoken=1") and not nxt.params
    with pytest.raises(ConnectorError) as excinfo:
        api.mail_delta("pm-intake@pm.example.com", "inbox", page_size=50, cursor="https://evil.example.com/steal")
    assert excinfo.value.code == "validation_failed"  # cursors can never redirect us off Graph (SSRF)
    drive = api.drive_delta("b!DEMOdrive0001", page_size=20)
    assert drive.url == "https://graph.microsoft.com/v1.0/drives/b!DEMOdrive0001/root/delta"
    used = api.worksheet_used_range("b!DEMOdrive0001", "01DEMOTRACKER0001", "Tracker")
    assert used.url.endswith("/items/01DEMOTRACKER0001/workbook/worksheets/Tracker/usedRange(valuesOnly=true)")
    send = api.send_mail("pm-agent-outreach@pm.example.com", to=["pm@slb.com"], cc=[], subject="Hi", body="Text",
                         action_id="act_1")
    assert send.method == "POST" and send.url.endswith("/users/pm-agent-outreach@pm.example.com/sendMail")
    message = send.json_body["message"]
    assert message["toRecipients"] == [{"emailAddress": {"address": "pm@slb.com"}}]
    assert {"name": "X-PM-Agent-Action-Id", "value": "act_1"} in message["internetMessageHeaders"]
    assert msgraph.excel_range_permalink("https://x.example.com/a.xlsx", "Tracker", "A2:G2") == \
        "https://x.example.com/a.xlsx?web=1&activeCell=%27Tracker%27%21A2%3AG2"


def test_fixture_graph_connectors(redactor: LocalRedactor, entities: EntityResolver) -> None:
    mail = msgraph.FixtureGraphMailConnector(redactor, entities=entities)
    signals = mail.poll(None).signals
    assert [s.source_record_id for s in signals] == [m["id"] for m in _mail()]
    assert signals[1].parent_signal_id == signals[0].signal_id
    assert signals[2].parent_signal_id is None and signals[3].parent_signal_id is None
    assert mail.get("AAMkADEMO-P2-MSG-002") == signals[3]
    drive = msgraph.FixtureGraphDriveConnector(redactor, entities=entities)
    drive_signals = drive.poll(None).signals
    assert {s.record_type for s in drive_signals} == {RecordType.TRACKER_ROW, RecordType.DOCUMENT}
    assert drive.item_text("01DEMODOC0002") is not None
    assert len(drive.workbook_rows("01DEMOTRACKER0001")) == 2


def test_http_graph_connectors_fail_loudly_until_q13(settings: Settings, redactor: LocalRedactor) -> None:
    for connector_cls in (msgraph.HttpGraphMailConnector, msgraph.HttpGraphDriveConnector):
        with pytest.raises(PlaceholderNotImplemented) as excinfo:
            connector_cls.from_settings(settings, redactor).poll(None)
        assert excinfo.value.question_id == "Q-13"


# ------------------------------------------------------------------------------------------------- Figma
def test_figma_library_normalization_and_search(redactor: LocalRedactor) -> None:
    connector = fixture_figma_connector(redactor)
    components = connector.components()
    assert [c.dls_key for c in components] == ["dls:button/primary", "dls:card/evidence", "dls:table/default"]
    assert components[0].kind == "component" and components[0].file_key == "DLSdemoLibraryKey01"
    sets = connector.component_sets()
    assert sets[0].kind == "component_set" and sets[0].name == "Button"
    assert connector.search_dls("table status column")[0].name == "Table/Default"
    assert connector.get_component("c0ffee00000000000000000000000000000000c1").name == "Card/Evidence"  # type: ignore[union-attr]
    assert connector.get_component("missing") is None
    nodes = connector.file_nodes("DLSdemoLibraryKey01", ["10:1"])
    assert list(nodes["nodes"]) == ["10:1"]
    images = connector.render_images("DLSdemoLibraryKey01", ["10:1", "20:1"])
    assert set(images) == {"10:1", "20:1"} and all(u.startswith("https://") for u in images.values())


def test_figma_comment_signals(redactor: LocalRedactor) -> None:
    signals = fixture_figma_connector(redactor).poll(None).signals
    root, reply = signals
    assert root.source_system is SourceSystem.FIGMA and root.record_type is RecordType.FIGMA_COMMENT
    assert root.permalink == "https://www.figma.com/design/PROTOdemoFileKey01?node-id=30-1#700000001"
    assert reply.parent_signal_id == root.signal_id
    assert figma.figma_node_permalink("KEY", "1:2") == "https://www.figma.com/design/KEY?node-id=1-2"


def test_figma_request_builders() -> None:
    api = figma.FigmaApi()
    request = api.team_components("1000000000000000001", page_size=30, after=12)
    assert request.url == "https://api.figma.com/v1/teams/1000000000000000001/components"
    assert dict(request.params) == {"page_size": "30", "after": "12"}
    assert api.team_component_sets("1").url.endswith("/v1/teams/1/component_sets")
    assert api.component("abc").url.endswith("/v1/components/abc")
    nodes = api.file_nodes("FILE", ["1:2", "3:4"], depth=2)
    assert nodes.url.endswith("/v1/files/FILE/nodes") and dict(nodes.params) == {"ids": "1:2,3:4", "depth": "2"}
    images = api.images("FILE", ["1:2"], fmt="png", scale=2)
    assert dict(images.params) == {"ids": "1:2", "format": "png", "scale": "2"}
    post = api.post_comment("FILE", "Looks good [pm-agent:act_1]", node_id="1:2")
    assert post.method == "POST" and post.url.endswith("/v1/files/FILE/comments")
    assert post.json_body == {"message": "Looks good [pm-agent:act_1]",
                              "client_meta": {"node_id": "1:2", "node_offset": {"x": 0, "y": 0}}}
    with pytest.raises(ValueError):
        api.images("FILE", ["1:2"], fmt="gif")


def test_http_figma_connector_fails_loudly_until_q02(settings: Settings, redactor: LocalRedactor) -> None:
    with pytest.raises(PlaceholderNotImplemented) as excinfo:
        figma.HttpFigmaConnector.from_settings(settings, redactor).components()
    assert excinfo.value.question_id == "Q-02"


# ---------------------------------------------------------------------------------------------------- KB
def test_fixture_kb_adapter_contract(redactor: LocalRedactor) -> None:
    adapter = fixture_kb_adapter()
    caps = adapter.capabilities()
    assert set(caps) == {"native_draft", "approval_workflow", "versioning", "labels"}
    hits = adapter.search("standardized log empty mapping file", top=5)
    assert hits[0].article_id == "KB0001001" and hits[0].url.startswith("https://")
    assert [h.article_id for h in adapter.search("witsml", filters={"labels": "how-to"})] == ["KB0001002"]
    article = adapter.get("KB0001001")
    assert article.version == "3" and article.title.startswith("Standardized log channel")
    with pytest.raises(LookupError):
        adapter.get("KB404")
    assert not adapter.validate({"title": "", "body": "x"}).ok
    draft = {"title": "TVDHD empty after late mapping upload", "body": "Upload the mapping file again."}
    assert adapter.validate(draft).ok
    ref = adapter.create_draft(draft)
    assert ref.state == "draft" and ref.url.startswith("https://")
    published = adapter.publish(ref)
    assert isinstance(published, kb.ExternalRef) and published.state == "published"
    adapter.retire(published)
    assert adapter.get(published.external_id).state == "retired"
    workflow = fixture_kb_adapter(approval_workflow=True)
    pending = workflow.publish(workflow.create_draft(draft))
    assert isinstance(pending, kb.PublishRequiresWorkflow) and pending.ref.state == "in_review"
    signal = kb.normalize_kb_article(article, redactor=redactor)
    assert signal.source_system is SourceSystem.KB and signal.record_type is RecordType.KB_ARTICLE
    assert (signal.source_record_id, signal.source_version, signal.permalink) == ("KB0001001", "3", article.url)


def test_http_kb_adapter_fails_loudly_until_q01(settings: Settings) -> None:
    adapter = kb.HttpKbAdapter.from_settings(settings)
    with pytest.raises(PlaceholderNotImplemented) as excinfo:
        adapter.search("anything")
    assert excinfo.value.question_id == "Q-01"


# ----------------------------------------------------------------------------------------- golden set
@pytest.fixture(name="golden", scope="module")
def _golden(redactor: LocalRedactor, settings: Settings) -> dict[SourceSystem, list[Signal]]:
    connectors = golden_connectors(redactor, settings=settings)
    assert set(connectors) == {SourceSystem.ADO, SourceSystem.SERVICENOW, SourceSystem.PAGERDUTY,
                               SourceSystem.EMAIL, SourceSystem.ONEDRIVE}
    polled: dict[SourceSystem, list[Signal]] = {}
    for source, connector in connectors.items():
        result = connector.poll(None)
        assert result.status is ConnectorStatus.OK and connector.source is source
        assert all(s.source_system is source for s in result.signals)
        polled[source] = result.signals
    return polled


def _index(golden: dict[SourceSystem, list[Signal]]) -> dict[tuple[str, str], Signal]:
    return {(s.source_system.value, s.source_record_id): s for signals in golden.values() for s in signals}


def test_golden_set_covers_manifest_records(golden: dict[SourceSystem, list[Signal]]) -> None:
    manifest = load_golden_manifest()
    index = _index(golden)
    for problem in manifest["problems"].values():
        for record in problem["records"]:
            signal = index.get((record["system"], record["record_id"]))
            assert signal is not None, record
            assert signal.record_type.value == record["record_type"], record
    for record in manifest["noise"]:
        assert (record["system"], record["record_id"]) in index


def test_golden_set_is_redacted(golden: dict[SourceSystem, list[Signal]]) -> None:
    manifest = load_golden_manifest()
    index = _index(golden)
    for signal in index.values():
        _no_secret(signal)
        for marker in manifest["raw_pii_markers"]:
            assert marker not in signal.raw_text and marker not in (signal.title or ""), (signal.source_record_id,
                                                                                          marker)
    for item in manifest["secrets"]:
        findings = index[(item["system"], item["record_id"])].redaction
        assert findings is not None and findings.findings.get(item["info_type"], 0) >= 1, item
    for item in manifest["pii"]:
        findings = index[(item["system"], item["record_id"])].redaction
        assert findings is not None and all(findings.findings.get(t, 0) >= 1 for t in item["info_types"]), item


def test_golden_set_has_exact_and_near_duplicates(golden: dict[SourceSystem, list[Signal]],
                                                  settings: Settings) -> None:
    manifest = load_golden_manifest()
    counts = Counter(s.signal_id for signals in golden.values() for s in signals)
    for dup in manifest["exact_duplicates"]:
        signal_id = deterministic_signal_id(SourceSystem(dup["system"]), dup["record_id"], dup["source_version"])
        assert counts[signal_id] == dup["deliveries"]
    index = _index(golden)
    for pair in manifest["near_duplicates"]:
        a = index[(pair["a"]["system"], pair["a"]["record_id"])]
        b = index[(pair["b"]["system"], pair["b"]["record_id"])]
        threshold = max(pair["min_jaccard"], settings.correlation.signal_minhash_jaccard)
        assert jaccard_exact(a.raw_text, b.raw_text) >= threshold
        assert jaccard_minhash(a.raw_text, b.raw_text) >= threshold  # what W1 uses for twins
        assert a.signal_id != b.signal_id and a.source_system is not b.source_system


def test_golden_set_hard_links_and_children(golden: dict[SourceSystem, list[Signal]]) -> None:
    manifest = load_golden_manifest()
    index = _index(golden)
    for link in manifest["hard_links"]:
        source = index[(link["from"]["system"], link["from"]["record_id"])]
        assert any(r.system.value == link["to"]["system"] and r.record_id == link["to"]["record_id"]
                   and r.pattern_id == link["pattern_id"] for r in source.cross_refs), link
        assert (link["to"]["system"], link["to"]["record_id"]) in index
    by_id = {s.signal_id: s for s in index.values()}
    children = [s for s in index.values() if s.record_type in (RecordType.COMMENT, RecordType.JOURNAL_NOTE)]
    assert children and all(s.parent_signal_id in by_id for s in children)


def test_golden_set_readiness_gaps_and_entities(golden: dict[SourceSystem, list[Signal]]) -> None:
    manifest = load_golden_manifest()
    index = _index(golden)
    p1_text = " ".join(index[(r["system"], r["record_id"])].raw_text for r in manifest["problems"]["P1"]["records"])
    assert "acceptance criteria" not in p1_text.lower()  # the readiness checklist must find this gap
    assert "Acceptance criteria" in index[("ado", "9100456")].raw_text
    assert index[("pagerduty", "90000123")].customer.name == "Contoso Energy"
    assert {index[("email", "AAMkADEMO-P2-MSG-001")].customer.name,
            index[("email", "AAMkADEMO-P2-MSG-002")].customer.name} == set(manifest["problems"]["P2"]["customers"])


def test_golden_signals_are_citable(golden: dict[SourceSystem, list[Signal]]) -> None:
    for signal in _index(golden).values():
        assert signal.permalink.startswith("https://")
        chunks = chunk_signal(signal)
        assert chunks and all(signal.raw_text[c.char_start:c.char_end] == c.text for c in chunks)


def test_fixture_root_is_overridable(monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
                                     redactor: LocalRedactor) -> None:
    assert FIXTURE_ROOT.is_dir() and (FIXTURE_ROOT / "golden_manifest.json").is_file()
    with pytest.raises(FileNotFoundError):
        golden_connectors(redactor, root=tmp_path / "missing")


# ------------------------------------------------------------------------------------- HTTP transport
@pytest.mark.parametrize(("status", "code"), [(400, "validation_failed"), (401, "auth_failed"), (403, "auth_failed"),
                                              (404, "not_found"), (422, "validation_failed"), (429, "rate_limited"),
                                              (500, "unavailable"), (503, "unavailable")])
def test_error_for_status(status: int, code: str) -> None:
    error = error_for_status(status, {"Retry-After": "7"})
    assert error.code == code
    assert error.retry_after_seconds == (7.0 if status in (429, 503) else None)


def _mock_client(handler: Callable[[httpx.Request], httpx.Response]) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_httpx_transport_success_and_typed_errors() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path.endswith("/limited"):
            return httpx.Response(429, headers={"Retry-After": "3"})
        return httpx.Response(200, json={"ok": True})

    transport = HttpxTransport(_mock_client(handler), credentials=lambda: {"Authorization": "Bearer test-only"})
    ok = transport.send(HttpRequest(method="GET", url="https://api.example.invalid/ok", params=(("a", "1"),),
                                    headers={"Accept": "application/json"}))
    assert ok == {"ok": True}
    assert seen[0].headers["Authorization"] == "Bearer test-only" and seen[0].url.params["a"] == "1"
    with pytest.raises(ConnectorError) as excinfo:
        transport.send(HttpRequest(method="GET", url="https://api.example.invalid/limited"))
    assert excinfo.value.code == "rate_limited" and excinfo.value.retry_after_seconds == 3.0

    def broken(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom", request=request)

    with pytest.raises(ConnectorError) as excinfo:
        HttpxTransport(_mock_client(broken)).send(HttpRequest(method="GET", url="https://api.example.invalid/x"))
    assert excinfo.value.code == "unavailable"


def test_unconfigured_transport_names_the_open_question() -> None:
    with pytest.raises(PlaceholderNotImplemented) as excinfo:
        UnconfiguredTransport("Q-07", "pagerduty").send(HttpRequest(method="GET", url="https://api.example.invalid"))
    assert excinfo.value.question_id == "Q-07"
