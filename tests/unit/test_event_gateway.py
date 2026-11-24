"""Unit tests for the signal-event-gateway (docs/09 §9 webhook ingress; docs/08 §8 T8 webhook spoofing / flooding).

Synthetic data only — customers Contoso Energy (CNE), Fabrikam Petroleum (FPC), Northwind Offshore (NWO); hosts
example.invalid / example.com. Every secret below is a throw-away test value.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import sys
from collections.abc import Iterator, MutableMapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import fields
from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from pm_agent.event_gateway import (
    MAX_BODY_BYTES,
    ChangeHint,
    HintPublisher,
    InMemoryHintPublisher,
    PubSubHintPublisher,
    WebhookSecrets,
    create_app,
)
from pm_agent.event_gateway import secrets as gateway_secrets
from pm_agent.placeholders import PlaceholderNotImplemented, is_placeholder, parse_placeholder, placeholder
from pm_agent.settings import Settings, load_settings

NOW = datetime(2026, 11, 24, 10, 15, tzinfo=UTC)
LEAK = "Password=hunter2 customer complaint"  # body text that must never reach a hint or a log line

ADO_USER = "svc-ado-hooks"
ADO_PASSWORD = "test-only-ado-basic-password"
ADO_SHARED = "test-only-ado-shared-secret"
SNOW_SECRET = "test-only-servicenow-hmac-secret"
PD_NEW = "test-only-pagerduty-secret-new"
PD_OLD = "test-only-pagerduty-secret-old"
GRAPH_STATE = "test-only-graph-client-state"
FIGMA_PASSCODE = "test-only-figma-passcode"
ALL_SECRET_VALUES = (ADO_PASSWORD, ADO_SHARED, SNOW_SECRET, PD_NEW, PD_OLD, GRAPH_STATE, FIGMA_PASSCODE)

SECRETS = WebhookSecrets(
    ado_basic_username=ADO_USER,
    ado_basic_password=ADO_PASSWORD,
    ado_shared_secret=ADO_SHARED,
    servicenow_hmac_secret=SNOW_SECRET,
    pagerduty_signing_secrets=(PD_NEW, PD_OLD),
    msgraph_client_state=GRAPH_STATE,
    figma_passcode=FIGMA_PASSCODE,
)

SYS_ID = "0123456789abcdef0123456789abcdef"
PD_EVENT_ID = "01HZZEVENT0000000000000001"
PD_INCIDENT = "PINC0042"
GRAPH_SUBSCRIPTION = "11111111-2222-4333-8444-555555555555"
GRAPH_RESOURCE = "Users/0b1c2d3e-0000-4000-8000-00000000abcd/Messages/AAMkADExampleMessageId="
FIGMA_FILE_KEY = "FiGmAkEy0042"
GRAPH_TOKEN = ("Validation: Testing client application reachability for subscription "
               "Request-Id: 3c1e0f5a-0000-4000-8000-000000000001")

HINT = ChangeHint(system="pagerduty", event_type="incident.annotated", received_utc=NOW, record_id=PD_INCIDENT,
                  attributes={"event_id": PD_EVENT_ID, "resource_type": "incident"})


# ---------------------------------------------------------------------------------------------------------- helpers
def _body(payload: Any) -> bytes:
    return json.dumps(payload).encode("utf-8")


def _hmac_hex(secret: str, body: bytes) -> str:
    return hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()


def _basic(user: str, password: str) -> str:
    return "Basic " + base64.b64encode(f"{user}:{password}".encode("utf-8")).decode("ascii")


def _ado_payload(event_type: str = "workitem.updated") -> dict[str, Any]:
    resource: dict[str, Any] = {
        "id": 7, "workItemId": 9100123, "rev": 12,
        "fields": {"System.Title": f"Contoso Energy WELL-0042(h) pump trips - {LEAK}"},
        "url": "https://example.invalid/_apis/wit/workItems/9100123/updates/7",
    }
    if event_type == "workitem.created":
        resource = {"id": 9100124, "rev": 1, "fields": {"System.Title": LEAK}}
    return {
        "subscriptionId": "00000000-0000-4000-8000-000000000001", "notificationId": 3, "id": "evt-ado-0001",
        "eventType": event_type, "publisherId": "tfs",
        "message": {"text": f"Bug #9100123 updated: {LEAK}"}, "detailedMessage": {"text": LEAK},
        "resource": resource, "createdDate": "2026-11-24T10:14:00Z",
    }


def _snow_payload(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "table": "incident", "sys_id": SYS_ID, "event_type": "updated", "number": "INC0090001",
        "short_description": LEAK, "description": f"Fabrikam Petroleum FPC CS9000123: {LEAK}",
    }
    payload.update(overrides)
    return payload


def _pd_payload(event_type: str = "incident.triggered") -> dict[str, Any]:
    data: dict[str, Any] = {
        "id": PD_INCIDENT, "type": "incident", "number": 90000123, "status": "triggered",
        "title": f"Northwind Offshore NWO PD #90000123: {LEAK}",
        "html_url": f"https://example.invalid/incidents/{PD_INCIDENT}",
    }
    if event_type == "incident.annotated":  # PagerDuty v3: resource_type stays "incident", data is the note [K]
        data = {
            "id": "PNOTE01", "type": "incident_note", "content": LEAK, "trimmed": False,
            "incident": {"id": PD_INCIDENT, "type": "incident_reference", "summary": LEAK,
                         "html_url": f"https://example.invalid/incidents/{PD_INCIDENT}"},
        }
    return {"event": {"id": PD_EVENT_ID, "event_type": event_type, "resource_type": "incident",
                      "occurred_at": "2026-11-24T10:14:59.000Z", "agent": None, "client": None, "data": data}}


def _graph_item(client_state: str = GRAPH_STATE, **overrides: Any) -> dict[str, Any]:
    item: dict[str, Any] = {
        "subscriptionId": GRAPH_SUBSCRIPTION, "subscriptionExpirationDateTime": "2026-11-25T10:00:00Z",
        "clientState": client_state, "changeType": "created", "resource": GRAPH_RESOURCE,
        "resourceData": {"@odata.type": "#Microsoft.Graph.Message", "id": "AAMkADExampleMessageId=", "subject": LEAK},
        "tenantId": "aaaaaaaa-0000-4000-8000-000000000000",
    }
    item.update(overrides)
    return item


def _figma_payload(event_type: str = "FILE_COMMENT", passcode: str = FIGMA_PASSCODE) -> dict[str, Any]:
    payload: dict[str, Any] = {"event_type": event_type, "passcode": passcode, "timestamp": "2026-11-24T10:14:00Z",
                               "webhook_id": "22"}
    if event_type != "PING":
        payload.update({"file_key": FIGMA_FILE_KEY, "file_name": f"CNE dashboard - {LEAK}",
                        "comment": [{"text": LEAK}], "triggered_by": {"id": "1", "handle": "designer"}})
    return payload


def _post_snow(client: TestClient, body: bytes, signature: str | None = None) -> Any:
    headers = {"Content-Type": "application/json",
               "X-ServiceNow-Signature": _hmac_hex(SNOW_SECRET, body) if signature is None else signature}
    return client.post("/webhooks/servicenow", content=body, headers=headers)


def _post_pd(client: TestClient, body: bytes, signature_header: str | None) -> Any:
    headers = {"Content-Type": "application/json"}
    if signature_header is not None:
        headers["X-PagerDuty-Signature"] = signature_header
    return client.post("/webhooks/pagerduty", content=body, headers=headers)


def _http_scope(path: str, headers: list[tuple[bytes, bytes]]) -> dict[str, Any]:
    return {"type": "http", "asgi": {"version": "3.0", "spec_version": "2.3"}, "http_version": "1.1",
            "method": "POST", "scheme": "http", "path": path, "raw_path": path.encode("ascii"), "query_string": b"",
            "root_path": "", "headers": headers, "client": ("203.0.113.7", 40000), "server": ("testserver", 80),
            "state": {}}


class _FailingPublisher:
    def publish(self, hint: ChangeHint) -> None:
        raise RuntimeError(f"pubsub unavailable while publishing {hint.record_id}")


class _FakeFuture:
    def __init__(self) -> None:
        self.timeout: float | None = None

    def result(self, timeout: float | None = None) -> str:
        self.timeout = timeout
        return "message-id-1"


class _FakePubSubClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, bytes, dict[str, str]]] = []
        self.futures: list[_FakeFuture] = []

    def publish(self, topic: str, data: bytes, **attributes: str) -> _FakeFuture:
        self.calls.append((topic, data, attributes))
        future = _FakeFuture()
        self.futures.append(future)
        return future


# --------------------------------------------------------------------------------------------------------- fixtures
@pytest.fixture(name="settings")
def fixture_settings() -> Settings:
    return load_settings(environ={"PM_AGENT_ENV": "local"})


@pytest.fixture(name="publisher")
def fixture_publisher() -> InMemoryHintPublisher:
    return InMemoryHintPublisher()


@pytest.fixture(name="client")
def fixture_client(settings: Settings, publisher: InMemoryHintPublisher) -> TestClient:
    return TestClient(create_app(settings, publisher, SECRETS, clock=lambda: NOW))


# ----------------------------------------------------------------------------------------------------------- basics
def test_healthz(client: TestClient) -> None:
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "component": "signal-event-gateway"}


def test_public_ingress_exposes_no_api_docs(client: TestClient) -> None:
    for path in ("/docs", "/redoc", "/openapi.json"):
        assert client.get(path).status_code == 404


def test_default_body_cap_is_256_kib() -> None:
    assert MAX_BODY_BYTES == 256 * 1024


# -------------------------------------------------------------------------------------------------- Azure DevOps
def test_ado_basic_auth_publishes_identifier_only_hint(client: TestClient, publisher: InMemoryHintPublisher) -> None:
    response = client.post("/webhooks/ado", content=_body(_ado_payload()),
                           headers={"Authorization": _basic(ADO_USER, ADO_PASSWORD),
                                    "Content-Type": "application/json"})
    assert response.status_code == 202
    assert response.json() == {"accepted": 1}
    assert publisher.hints == [ChangeHint(system="ado", event_type="workitem.updated", received_utc=NOW,
                                          record_id="9100123", attributes={"rev": "12"})]


def test_ado_shared_secret_header_and_created_event_uses_resource_id(
        client: TestClient, publisher: InMemoryHintPublisher) -> None:
    response = client.post("/webhooks/ado", content=_body(_ado_payload("workitem.created")),
                           headers={"X-PM-Agent-Webhook-Secret": ADO_SHARED})
    assert response.status_code == 202
    assert publisher.hints == [ChangeHint(system="ado", event_type="workitem.created", received_utc=NOW,
                                          record_id="9100124", attributes={"rev": "1"})]


def test_ado_basic_scheme_is_case_insensitive(client: TestClient, publisher: InMemoryHintPublisher) -> None:
    authorization = "basic " + _basic(ADO_USER, ADO_PASSWORD).removeprefix("Basic ")
    response = client.post("/webhooks/ado", content=_body(_ado_payload("workitem.commented")),
                           headers={"Authorization": authorization})
    assert response.status_code == 202
    assert [hint.event_type for hint in publisher.hints] == ["workitem.commented"]


@pytest.mark.parametrize("headers", [
    {},
    {"Authorization": _basic(ADO_USER, "wrong-password")},
    {"Authorization": _basic("someone-else", ADO_PASSWORD)},
    {"Authorization": "Bearer " + ADO_PASSWORD},
    {"X-PM-Agent-Webhook-Secret": "wrong-shared-secret"},
    {"X-PM-Agent-Webhook-Secret": ADO_SHARED + "x"},
    {"Authorization": _basic(ADO_USER, ADO_SHARED)},
])
def test_ado_bad_credentials_rejected_without_publishing(
        client: TestClient, publisher: InMemoryHintPublisher, headers: dict[str, str]) -> None:
    response = client.post("/webhooks/ado", content=_body(_ado_payload()), headers=headers)
    assert response.status_code == 401
    assert response.json() == {"error": "unauthorized"}
    assert publisher.hints == []


def test_ado_unconfigured_secrets_fail_closed(settings: Settings, publisher: InMemoryHintPublisher) -> None:
    defaults = WebhookSecrets()
    client = TestClient(create_app(settings, publisher, defaults, clock=lambda: NOW))
    # The placeholder tokens are public (they are in the repo): presenting them must never authenticate.
    for headers in ({"Authorization": _basic(defaults.ado_basic_username, defaults.ado_basic_password)},
                    {"X-PM-Agent-Webhook-Secret": defaults.ado_shared_secret}):
        assert client.post("/webhooks/ado", content=_body(_ado_payload()), headers=headers).status_code == 401
    empty = WebhookSecrets(ado_basic_username="", ado_basic_password="", ado_shared_secret="")
    client = TestClient(create_app(settings, publisher, empty, clock=lambda: NOW))
    for headers in ({"Authorization": _basic("", "")}, {"X-PM-Agent-Webhook-Secret": ""}):
        assert client.post("/webhooks/ado", content=_body(_ado_payload()), headers=headers).status_code == 401
    assert publisher.hints == []


def test_ado_missing_work_item_id_is_bad_request(client: TestClient, publisher: InMemoryHintPublisher) -> None:
    payload = _ado_payload()
    payload["resource"] = {"rev": 3, "fields": {}}
    response = client.post("/webhooks/ado", content=_body(payload), headers={"X-PM-Agent-Webhook-Secret": ADO_SHARED})
    assert response.status_code == 400
    assert response.json() == {"error": "bad_request"}
    assert publisher.hints == []


def test_every_vendor_fails_closed_when_secrets_are_placeholders(
        settings: Settings, publisher: InMemoryHintPublisher) -> None:
    defaults = WebhookSecrets()
    client = TestClient(create_app(settings, publisher, defaults, clock=lambda: NOW))
    snow = _body(_snow_payload())
    assert _post_snow(client, snow, _hmac_hex(defaults.servicenow_hmac_secret, snow)).status_code == 401
    pd = _body(_pd_payload())
    assert _post_pd(client, pd, "v1=" + _hmac_hex(defaults.pagerduty_signing_secrets[0], pd)).status_code == 401
    graph = _body({"value": [_graph_item(client_state=defaults.msgraph_client_state)]})
    assert client.post("/webhooks/msgraph", content=graph).status_code == 401
    figma = _body(_figma_payload(passcode=defaults.figma_passcode))
    assert client.post("/webhooks/figma", content=figma).status_code == 401
    assert publisher.hints == []


# ----------------------------------------------------------------------------------------------------- ServiceNow
def test_servicenow_hmac_publishes_table_and_sys_id(client: TestClient, publisher: InMemoryHintPublisher) -> None:
    response = _post_snow(client, _body(_snow_payload()))
    assert response.status_code == 202
    assert response.json() == {"accepted": 1}
    assert publisher.hints == [ChangeHint(system="servicenow", event_type="updated", received_utc=NOW,
                                          record_id=SYS_ID, attributes={"table": "incident"})]


def test_servicenow_accepts_sha256_prefix(client: TestClient, publisher: InMemoryHintPublisher) -> None:
    body = _body(_snow_payload(table="sc_req_item"))
    response = _post_snow(client, body, "sha256=" + _hmac_hex(SNOW_SECRET, body))
    assert response.status_code == 202
    assert publisher.hints[0].attributes == {"table": "sc_req_item"}


def test_servicenow_tampered_body_rejected(client: TestClient, publisher: InMemoryHintPublisher) -> None:
    signed = _body(_snow_payload())
    tampered = _body(_snow_payload(sys_id="fedcba9876543210fedcba9876543210"))
    response = _post_snow(client, tampered, _hmac_hex(SNOW_SECRET, signed))
    assert response.status_code == 401
    assert response.json() == {"error": "unauthorized"}
    assert publisher.hints == []


@pytest.mark.parametrize("signature", ["", "sha256=", "not-hex", _hmac_hex("some-other-secret", b"{}")])
def test_servicenow_bad_signature_rejected(
        client: TestClient, publisher: InMemoryHintPublisher, signature: str) -> None:
    assert _post_snow(client, _body(_snow_payload()), signature).status_code == 401
    assert client.post("/webhooks/servicenow", content=_body(_snow_payload())).status_code == 401  # no header
    assert publisher.hints == []


@pytest.mark.parametrize("override", [
    {"sys_id": "not-a-sys-id"}, {"sys_id": SYS_ID.upper()}, {"sys_id": 12345}, {"table": "incident; drop"},
    {"table": ""}, {"table": None}, {"event_type": "has space"}, {"event_type": None},
])
def test_servicenow_invalid_identifiers_are_bad_requests(
        client: TestClient, publisher: InMemoryHintPublisher, override: dict[str, Any]) -> None:
    response = _post_snow(client, _body(_snow_payload(**override)))
    assert response.status_code == 400
    assert response.json() == {"error": "bad_request"}
    assert publisher.hints == []


# ------------------------------------------------------------------------------------------------------ PagerDuty
def test_pagerduty_signed_incident_event(client: TestClient, publisher: InMemoryHintPublisher) -> None:
    body = _body(_pd_payload())
    response = _post_pd(client, body, "v1=" + _hmac_hex(PD_NEW, body))
    assert response.status_code == 202
    assert publisher.hints == [ChangeHint(system="pagerduty", event_type="incident.triggered", received_utc=NOW,
                                          record_id=PD_INCIDENT,
                                          attributes={"event_id": PD_EVENT_ID, "resource_type": "incident"})]


def test_pagerduty_secret_rotation_any_listed_signature_may_match(
        client: TestClient, publisher: InMemoryHintPublisher) -> None:
    body = _body(_pd_payload("incident.resolved"))
    header = f"v1={'0' * 64}, v1={_hmac_hex(PD_OLD, body)},v2=ignored-scheme"
    assert _post_pd(client, body, header).status_code == 202
    assert [hint.event_type for hint in publisher.hints] == ["incident.resolved"]


@pytest.mark.parametrize("resource_type", ["incident", "incident_note"])
def test_pagerduty_annotated_event_uses_parent_incident_id(
        client: TestClient, publisher: InMemoryHintPublisher, resource_type: str) -> None:
    payload = _pd_payload("incident.annotated")
    payload["event"]["resource_type"] = resource_type
    body = _body(payload)
    assert _post_pd(client, body, "v1=" + _hmac_hex(PD_NEW, body)).status_code == 202
    assert publisher.hints == [ChangeHint(system="pagerduty", event_type="incident.annotated", received_utc=NOW,
                                          record_id=PD_INCIDENT,
                                          attributes={"event_id": PD_EVENT_ID, "resource_type": resource_type})]


@pytest.mark.parametrize("make_header", [
    lambda body: None,
    lambda body: "",
    lambda body: "v1=" + _hmac_hex("not-the-secret", body),
    lambda body: "v0=" + _hmac_hex(PD_NEW, body),  # non-v1 schemes are ignored
    lambda body: _hmac_hex(PD_NEW, body),  # bare hex without a scheme
])
def test_pagerduty_bad_signatures_rejected(
        client: TestClient, publisher: InMemoryHintPublisher, make_header: Any) -> None:
    body = _body(_pd_payload())
    response = _post_pd(client, body, make_header(body))
    assert response.status_code == 401
    assert response.json() == {"error": "unauthorized"}
    assert publisher.hints == []


def test_pagerduty_ping_acknowledged_without_hint(client: TestClient, publisher: InMemoryHintPublisher) -> None:
    body = _body({"event": {"id": "01HZZPING00000000000000001", "event_type": "pagey.ping", "resource_type": "pagey",
                            "occurred_at": "2026-11-24T10:14:59.000Z", "agent": None, "client": "PagerDuty",
                            "data": {"message": "Hello from your friend Pagey!", "type": "ping"}}})
    response = _post_pd(client, body, "v1=" + _hmac_hex(PD_NEW, body))
    assert response.status_code == 200
    assert response.json() == {"accepted": 0}
    assert publisher.hints == []


# ---------------------------------------------------------------------------------------------- Microsoft Graph
@pytest.mark.parametrize("path", ["/webhooks/msgraph", "/webhooks/msgraph/lifecycle"])
def test_graph_validation_handshake_echoes_decoded_token(
        client: TestClient, publisher: InMemoryHintPublisher, path: str) -> None:
    response = client.post(f"{path}?validationToken={quote(GRAPH_TOKEN)}",
                           headers={"Content-Type": "text/plain; charset=utf-8"})
    assert response.status_code == 200
    assert response.text == GRAPH_TOKEN
    assert response.headers["content-type"].startswith("text/plain")
    assert response.headers["x-content-type-options"] == "nosniff"
    assert publisher.hints == []


@pytest.mark.parametrize("path", ["/webhooks/msgraph", "/webhooks/msgraph/lifecycle"])
def test_graph_validation_token_longer_than_1024_chars_rejected(
        client: TestClient, publisher: InMemoryHintPublisher, path: str) -> None:
    response = client.post(path, params={"validationToken": "x" * 1025})
    assert response.status_code == 400
    assert "x" * 1025 not in response.text
    assert client.post(path, params={"validationToken": "x" * 1024}).status_code == 200
    assert publisher.hints == []


def test_graph_notification_publishes_only_verified_items(
        client: TestClient, publisher: InMemoryHintPublisher) -> None:
    forged = _graph_item(client_state="forged-state", resource="Users/attacker/Messages/x", subscriptionId="evil")
    response = client.post("/webhooks/msgraph", content=_body({"value": [_graph_item(), forged, "not-an-object"]}))
    assert response.status_code == 202
    assert response.json() == {"accepted": 1}
    assert publisher.hints == [ChangeHint(system="msgraph", event_type="created", received_utc=NOW,
                                          resource=GRAPH_RESOURCE, attributes={"subscription_id": GRAPH_SUBSCRIPTION})]


def test_graph_wrong_client_state_is_unauthorized(client: TestClient, publisher: InMemoryHintPublisher) -> None:
    body = _body({"value": [_graph_item(client_state="forged-state"), {"subscriptionId": GRAPH_SUBSCRIPTION}]})
    response = client.post("/webhooks/msgraph", content=body)
    assert response.status_code == 401
    assert response.json() == {"error": "unauthorized"}
    assert publisher.hints == []


def test_graph_lifecycle_notification_uses_lifecycle_event(
        client: TestClient, publisher: InMemoryHintPublisher) -> None:
    item = {"subscriptionId": GRAPH_SUBSCRIPTION, "subscriptionExpirationDateTime": "2026-11-25T10:00:00Z",
            "tenantId": "aaaaaaaa-0000-4000-8000-000000000000", "clientState": GRAPH_STATE,
            "lifecycleEvent": "reauthorizationRequired"}
    response = client.post("/webhooks/msgraph/lifecycle", content=_body({"value": [item]}))
    assert response.status_code == 202
    assert publisher.hints == [ChangeHint(system="msgraph", event_type="reauthorizationRequired", received_utc=NOW,
                                          resource=GRAPH_SUBSCRIPTION,
                                          attributes={"subscription_id": GRAPH_SUBSCRIPTION})]


@pytest.mark.parametrize("payload", [
    {"value": [_graph_item(resource="Users/x/Messages/<script>")]},
    {"value": [_graph_item(changeType=None)]},
    {"value": "not-a-list"},
    {"notifications": []},
])
def test_graph_malformed_notifications_are_bad_requests(
        client: TestClient, publisher: InMemoryHintPublisher, payload: dict[str, Any]) -> None:
    assert client.post("/webhooks/msgraph", content=_body(payload)).status_code == 400
    assert publisher.hints == []


# ---------------------------------------------------------------------------------------------------------- Figma
def test_figma_passcode_publishes_file_key_hint(client: TestClient, publisher: InMemoryHintPublisher) -> None:
    response = client.post("/webhooks/figma", content=_body(_figma_payload()))
    assert response.status_code == 200  # Figma treats anything but 200 OK as a failed delivery and retries [K]
    assert response.json() == {"accepted": 1}
    assert publisher.hints == [ChangeHint(system="figma", event_type="FILE_COMMENT", received_utc=NOW,
                                          record_id=FIGMA_FILE_KEY, attributes={"webhook_id": "22"})]


def test_figma_numeric_webhook_id_is_stringified(client: TestClient, publisher: InMemoryHintPublisher) -> None:
    payload = _figma_payload("LIBRARY_PUBLISH")
    payload["webhook_id"] = 22
    assert client.post("/webhooks/figma", content=_body(payload)).status_code == 200
    assert publisher.hints[0].attributes == {"webhook_id": "22"}


def test_figma_ping_acknowledged_without_hint(client: TestClient, publisher: InMemoryHintPublisher) -> None:
    response = client.post("/webhooks/figma", content=_body(_figma_payload("PING")))
    assert response.status_code == 200
    assert response.json() == {"accepted": 0}
    assert publisher.hints == []


def test_figma_wrong_or_missing_passcode_rejected(client: TestClient, publisher: InMemoryHintPublisher) -> None:
    missing = {key: value for key, value in _figma_payload().items() if key != "passcode"}
    for payload in (_figma_payload(passcode="guess"), _figma_payload("PING", passcode="guess"), missing):
        response = client.post("/webhooks/figma", content=_body(payload))
        assert response.status_code == 401
        assert response.json() == {"error": "unauthorized"}
    assert publisher.hints == []


# ------------------------------------------------------------------------------------------- size caps & parsing
def test_declared_oversize_body_rejected_413(settings: Settings, publisher: InMemoryHintPublisher) -> None:
    client = TestClient(create_app(settings, publisher, SECRETS, max_body_bytes=1024, clock=lambda: NOW))
    response = _post_snow(client, _body(_snow_payload(description="x" * 2048)))
    assert response.status_code == 413
    assert response.json() == {"error": "payload_too_large"}
    assert publisher.hints == []


def test_streamed_oversize_body_without_content_length_rejected_413(
        settings: Settings, publisher: InMemoryHintPublisher) -> None:
    client = TestClient(create_app(settings, publisher, SECRETS, max_body_bytes=1024, clock=lambda: NOW))
    body = _body(_snow_payload(description="x" * 4096))

    def chunks() -> Iterator[bytes]:  # a generator body is sent chunked, without Content-Length
        for start in range(0, len(body), 512):
            yield body[start:start + 512]

    response = client.post("/webhooks/servicenow", content=chunks(),
                           headers={"X-ServiceNow-Signature": _hmac_hex(SNOW_SECRET, body)})
    assert response.status_code == 413
    assert publisher.hints == []


async def test_body_read_incrementally_and_abandoned_once_over_cap(
        settings: Settings, publisher: InMemoryHintPublisher) -> None:
    app = create_app(settings, publisher, SECRETS, max_body_bytes=1024, clock=lambda: NOW)
    pulls = 0
    sent: list[MutableMapping[str, Any]] = []

    async def receive() -> dict[str, Any]:
        nonlocal pulls
        pulls += 1
        return {"type": "http.request", "body": b"x" * 600, "more_body": pulls < 100}

    async def send(message: MutableMapping[str, Any]) -> None:
        sent.append(message)

    await app(_http_scope("/webhooks/servicenow", [(b"x-servicenow-signature", b"00")]), receive, send)
    assert sent[0]["type"] == "http.response.start"
    assert sent[0]["status"] == 413
    assert pulls <= 3  # stopped reading right after crossing the cap (the stream had 100 chunks)
    assert publisher.hints == []


async def test_invalid_content_length_header_is_bad_request(
        settings: Settings, publisher: InMemoryHintPublisher) -> None:
    app = create_app(settings, publisher, SECRETS, clock=lambda: NOW)
    sent: list[MutableMapping[str, Any]] = []

    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": b"{}", "more_body": False}

    async def send(message: MutableMapping[str, Any]) -> None:
        sent.append(message)

    await app(_http_scope("/webhooks/ado", [(b"content-length", b"abc")]), receive, send)
    assert sent[0]["status"] == 400
    assert publisher.hints == []


@pytest.mark.parametrize("raw", [b"{not json", b"[1, 2, 3]", b'"just a string"', b"", b"\xff\xfe\x00", b"[" * 50_000])
def test_malformed_json_is_bad_request(client: TestClient, publisher: InMemoryHintPublisher, raw: bytes) -> None:
    response = _post_snow(client, raw)  # validly signed garbage: verification passes, parsing fails
    assert response.status_code == 400
    assert response.json() == {"error": "bad_request"}
    ado = client.post("/webhooks/ado", content=raw, headers={"Authorization": _basic(ADO_USER, ADO_PASSWORD)})
    assert ado.status_code == 400
    assert publisher.hints == []


def test_publish_failure_returns_503_so_vendor_retries(settings: Settings) -> None:
    client = TestClient(create_app(settings, _FailingPublisher(), SECRETS, clock=lambda: NOW))
    response = _post_snow(client, _body(_snow_payload()))
    assert response.status_code == 503
    assert response.json() == {"error": "unavailable"}


# ------------------------------------------------------------------------------------- hints carry no body text
def test_hints_and_logs_never_carry_body_text_or_secrets(
        client: TestClient, publisher: InMemoryHintPublisher, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    ado = _body(_ado_payload())
    assert client.post("/webhooks/ado", content=ado,
                       headers={"Authorization": _basic(ADO_USER, ADO_PASSWORD)}).status_code == 202
    assert _post_snow(client, _body(_snow_payload())).status_code == 202
    pd = _body(_pd_payload("incident.annotated"))
    assert _post_pd(client, pd, "v1=" + _hmac_hex(PD_NEW, pd)).status_code == 202
    assert client.post("/webhooks/msgraph", content=_body({"value": [_graph_item()]})).status_code == 202
    assert client.post("/webhooks/figma", content=_body(_figma_payload())).status_code == 200
    # Failures are logged too, and must not echo what was sent either.
    rejected = client.post("/webhooks/ado", content=ado, headers={"Authorization": _basic(ADO_USER, LEAK)})
    assert rejected.status_code == 401
    assert _post_snow(client, _body(_snow_payload(sys_id=LEAK))).status_code == 400
    assert client.post("/webhooks/figma", content=_body(_figma_payload(passcode=LEAK))).status_code == 401

    assert len(publisher.hints) == 5
    for hint in publisher.hints:
        dumped = hint.model_dump_json()
        assert "hunter2" not in dumped
        assert "complaint" not in dumped
    assert [record for record in caplog.records if record.name.startswith("pm_agent.event_gateway")]
    for text in [caplog.text, *(record.getMessage() for record in caplog.records)]:
        assert "hunter2" not in text
        for secret in ALL_SECRET_VALUES:
            assert secret not in text


# ------------------------------------------------------------------------------------------------- ChangeHint model
@pytest.mark.parametrize("bad", ["", "has space", "tab\there", "new\nline", "trailing\n", "<script>", 'quo"te',
                                 "{json}", "x" * 513, "nul\x00byte", "nbsp\u00a0inside"])
def test_change_hint_rejects_non_identifier_strings(bad: str) -> None:
    with pytest.raises(ValidationError):
        ChangeHint(system="ado", event_type=bad, received_utc=NOW)
    for field_name in ("record_id", "resource"):
        with pytest.raises(ValidationError):
            ChangeHint.model_validate({"system": "ado", "event_type": "workitem.updated", "received_utc": NOW,
                                       field_name: bad})
    with pytest.raises(ValidationError):
        ChangeHint(system="ado", event_type="workitem.updated", received_utc=NOW, attributes={"rev": bad})
    with pytest.raises(ValidationError):
        ChangeHint(system="ado", event_type="workitem.updated", received_utc=NOW, attributes={bad: "1"})


def test_change_hint_is_frozen_strict_and_timezone_aware() -> None:
    hint = ChangeHint(system="figma", event_type="FILE_VERSION_UPDATE", received_utc=NOW, record_id=FIGMA_FILE_KEY)
    assert hint.attributes == {}
    assert ChangeHint(system="ado", event_type="e", received_utc=NOW, record_id="x" * 512).record_id == "x" * 512
    with pytest.raises(ValidationError):
        hint.event_type = "FILE_DELETE"  # type: ignore[misc]
    with pytest.raises(ValidationError):
        ChangeHint.model_validate({"system": "figma", "event_type": "PING", "received_utc": NOW, "body": "text"})
    with pytest.raises(ValidationError):
        ChangeHint.model_validate({"system": "jira", "event_type": "x", "received_utc": NOW})
    with pytest.raises(ValidationError):
        ChangeHint(system="figma", event_type="x", received_utc=datetime(2026, 11, 24, 10, 15))  # naive
    assert ChangeHint.model_validate_json(hint.model_dump_json()) == hint


def test_in_memory_publisher_is_thread_safe() -> None:
    publisher = InMemoryHintPublisher()
    typed: HintPublisher = publisher
    hints = [ChangeHint(system="ado", event_type="workitem.updated", received_utc=NOW, record_id=str(9100000 + i))
             for i in range(300)]
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(typed.publish, hints))
    assert sorted(str(hint.record_id) for hint in publisher.hints) == sorted(str(hint.record_id) for hint in hints)


# ------------------------------------------------------------------------------------------- Pub/Sub publisher
def test_pubsub_publisher_sends_hint_json_with_routing_attributes() -> None:
    fake = _FakePubSubClient()
    publisher: HintPublisher = PubSubHintPublisher("projects/chayan-genai-bb/topics/signal-change-hints",
                                                   client=fake, timeout_seconds=3.5)
    publisher.publish(HINT)
    assert len(fake.calls) == 1
    topic, data, attributes = fake.calls[0]
    assert topic == "projects/chayan-genai-bb/topics/signal-change-hints"
    assert isinstance(data, bytes)
    assert ChangeHint.model_validate_json(data) == HINT
    assert json.loads(data)["record_id"] == PD_INCIDENT
    assert attributes == {"system": "pagerduty", "event_type": "incident.annotated"}
    assert fake.futures[0].timeout == 3.5


def test_pubsub_publisher_from_settings_targets_project_topic(settings: Settings) -> None:
    publisher = PubSubHintPublisher.from_settings(settings)
    assert publisher.topic_path == f"projects/{settings.gcp.project_id}/topics/signal-change-hints"
    assert PubSubHintPublisher.from_settings(settings, topic="hints-dev").topic_path.endswith("/topics/hints-dev")


def test_pubsub_publisher_refuses_placeholder_topic(settings: Settings) -> None:
    fake = _FakePubSubClient()
    publisher = PubSubHintPublisher(f"projects/{placeholder('Q-10', 'gcp_project_id')}/topics/signal-change-hints",
                                    client=fake)
    with pytest.raises(PlaceholderNotImplemented) as exc:
        publisher.publish(HINT)
    assert exc.value.question_id == "Q-10"
    unresolved = settings.model_copy(
        update={"gcp": settings.gcp.model_copy(update={"project_id": placeholder("Q-10", "prod_project_id")})})
    with pytest.raises(PlaceholderNotImplemented):
        PubSubHintPublisher.from_settings(unresolved).publish(HINT)
    assert not fake.calls


def test_pubsub_publisher_without_library_is_a_tracked_gap(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "google.cloud.pubsub_v1", None)
    publisher = PubSubHintPublisher("projects/chayan-genai-bb/topics/signal-change-hints")
    with pytest.raises(PlaceholderNotImplemented) as exc:
        publisher.publish(HINT)
    assert exc.value.question_id == "Q-10"


# ------------------------------------------------------------------------------------------------ WebhookSecrets
def test_webhook_secret_defaults_are_tracked_placeholders() -> None:
    secrets = WebhookSecrets()
    expected = {"ado_basic_username": "Q-04", "ado_basic_password": "Q-04", "ado_shared_secret": "Q-04",
                "servicenow_hmac_secret": "Q-05", "msgraph_client_state": "Q-13", "figma_passcode": "Q-02"}
    for field_name, question_id in expected.items():
        value = getattr(secrets, field_name)
        assert is_placeholder(value), field_name
        parsed = parse_placeholder(value)
        assert parsed is not None and parsed[0] == question_id, field_name
    assert len(secrets.pagerduty_signing_secrets) == 1
    pd_token = parse_placeholder(secrets.pagerduty_signing_secrets[0])
    assert pd_token is not None and pd_token[0] == "Q-07"


def test_webhook_secrets_from_env_reads_secret_manager_projection() -> None:
    env = {
        "PM_AGENT_WEBHOOK_ADO_BASIC_USERNAME": ADO_USER,
        "PM_AGENT_WEBHOOK_ADO_BASIC_PASSWORD": ADO_PASSWORD,
        "PM_AGENT_WEBHOOK_ADO_SHARED_SECRET": ADO_SHARED,
        "PM_AGENT_WEBHOOK_SERVICENOW_HMAC_SECRET": SNOW_SECRET + "\n",  # Secret Manager values often end in a newline
        "PM_AGENT_WEBHOOK_PAGERDUTY_SIGNING_SECRETS": f" {PD_NEW} , {PD_OLD},, ",
        "PM_AGENT_WEBHOOK_MSGRAPH_CLIENT_STATE": GRAPH_STATE,
        "PM_AGENT_WEBHOOK_FIGMA_PASSCODE": FIGMA_PASSCODE,
    }
    assert WebhookSecrets.from_env(env) == SECRETS


def test_webhook_secrets_from_env_missing_or_blank_falls_back_to_placeholders() -> None:
    assert WebhookSecrets.from_env({}) == WebhookSecrets()
    blank = WebhookSecrets.from_env({"PM_AGENT_WEBHOOK_FIGMA_PASSCODE": "   ",
                                     "PM_AGENT_WEBHOOK_PAGERDUTY_SIGNING_SECRETS": " , "})
    assert blank == WebhookSecrets()


def test_webhook_secrets_from_env_defaults_to_process_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PM_AGENT_WEBHOOK_SERVICENOW_HMAC_SECRET", "from-process-env")
    assert WebhookSecrets.from_env().servicenow_hmac_secret == "from-process-env"


def test_webhook_secrets_repr_never_reveals_values() -> None:
    text = repr(SECRETS) + str(SECRETS)
    for secret in ALL_SECRET_VALUES:
        assert secret not in text


def test_webhook_secret_placeholders_are_listed_literally_for_the_registry() -> None:
    # tools/list_placeholders.py greps literal PLACEHOLDER tokens; the module docstring lists every default.
    defaults = WebhookSecrets()
    documented = gateway_secrets.__doc__ or ""
    for secret_field in fields(WebhookSecrets):
        value = getattr(defaults, secret_field.name)
        for token in value if isinstance(value, tuple) else (value,):
            assert token in documented, secret_field.name
