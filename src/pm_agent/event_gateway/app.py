"""signal-event-gateway HTTP app (GKE, public HTTPS LB + Cloud Armor): vendor webhooks in, change hints out.

docs/09 §9, docs/08 §8 T8. Pipeline for every ``POST /webhooks/<vendor>`` route::

    Content-Length over the cap ........................................ 413  (cheap pre-check)
    header-only checks (ADO credentials; signature header present and
      secret configured; Graph / Figma secret configured) .............. 401  body never read
    body streamed with a running byte count ............................ 413  never buffers more than cap + 1 chunk
    verification on the RAW bytes (HMAC) ............................... 401  {"error": "unauthorized"}, no detail
    JSON parse + identifier extraction ................................. 400  {"error": "bad_request"}
    publish identifier-only ChangeHint(s) .............................. 202  {"accepted": n}  (Figma: 200)
    publisher failure ................................................... 503  {"error": "unavailable"}, vendor retries

Graph and Figma carry their shared secret *inside* the JSON body (``clientState`` / ``passcode``), so for those two
the body is parsed before the comparison — and only when a secret is configured. Vendor test pings (Figma ``PING``,
PagerDuty ``pagey.ping``) are verified, then acknowledged with 200 and no hint. Graph ``validationToken`` handshakes
are echoed as ``text/plain`` with ``nosniff`` and publish nothing.

Logging: vendor, outcome code, HTTP status and byte length only — never bodies, header values or identifiers.
"""

import json
import logging
import re
from collections.abc import Awaitable, Callable
from contextlib import aclosing
from dataclasses import dataclass
from datetime import datetime
from typing import Any, TypeAlias

from fastapi import FastAPI
from pydantic import ValidationError
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import Headers
from starlette.requests import ClientDisconnect, Request
from starlette.responses import JSONResponse, PlainTextResponse, Response

from pm_agent.event_gateway.hints import ChangeHint, HintPublisher, HintSystem, is_identifier
from pm_agent.event_gateway.secrets import WebhookSecrets, is_configured
from pm_agent.event_gateway.verify import (
    ADO_SHARED_SECRET_HEADER,
    PAGERDUTY_SIGNATURE_HEADER,
    SERVICENOW_SIGNATURE_HEADER,
    verify_ado,
    verify_figma_passcode,
    verify_msgraph_client_state,
    verify_pagerduty,
    verify_servicenow,
)
from pm_agent.schemas.canonical import utcnow
from pm_agent.settings import Settings

COMPONENT = "signal-event-gateway"
MAX_BODY_BYTES = 256 * 1024
MAX_VALIDATION_TOKEN_CHARS = 1024

_LOG = logging.getLogger("pm_agent.event_gateway")
_CONTENT_LENGTH_RE = re.compile(r"[0-9]{1,19}")
_SERVICENOW_TABLE_RE = re.compile(r"[a-z0-9_]{1,80}")
_SERVICENOW_SYS_ID_RE = re.compile(r"[0-9a-f]{32}")
_FIGMA_PING = "PING"
_PAGERDUTY_PING = "pagey.ping"
_ERROR_CODES = {400: "bad_request", 401: "unauthorized", 413: "payload_too_large", 503: "unavailable"}

_Precheck: TypeAlias = Callable[[Headers], bool]
_Extractor: TypeAlias = Callable[[bytes, Headers, datetime], list[ChangeHint] | None]  # None = ping, no hint


class _Rejected(Exception):
    """Abort the request with a fixed, detail-free error body; ``outcome`` is the (data-free) log code."""

    def __init__(self, status_code: int, outcome: str, observed_bytes: int | None = None) -> None:
        super().__init__(outcome)
        self.status_code = status_code
        self.outcome = outcome
        self.observed_bytes = observed_bytes


def _unauthorized() -> _Rejected:
    return _Rejected(401, "unauthorized")


def _bad_request(outcome: str = "bad_request") -> _Rejected:
    return _Rejected(400, outcome)


@dataclass(frozen=True)
class _Route:
    path: str
    vendor: HintSystem
    precheck: _Precheck  # cheap, header-only necessary condition: False → 401 before the body is read
    extract: _Extractor  # full verification on the raw bytes, then identifier extraction
    success_status: int = 202
    graph_handshake: bool = False


def _parse_object(raw: bytes) -> dict[str, Any]:
    try:
        payload = json.loads(raw)
    except (ValueError, RecursionError):  # JSONDecodeError and UnicodeDecodeError are ValueErrors
        raise _bad_request("invalid_json") from None
    if not isinstance(payload, dict):
        raise _bad_request("invalid_json")
    return payload


def _object(container: dict[str, Any], key: str) -> dict[str, Any]:
    value = container.get(key)
    if not isinstance(value, dict):
        raise _bad_request()
    return value


def _identifier(value: object) -> str:
    """A JSON string or integer id that is identifier-like; anything else (text, bool, null, object) → 400."""
    if isinstance(value, int) and not isinstance(value, bool):
        value = str(value)
    if not isinstance(value, str) or not is_identifier(value):
        raise _bad_request()
    return value


def _hint(system: HintSystem, event_type: str, received: datetime, *, record_id: str | None = None,
          resource: str | None = None, attributes: dict[str, str] | None = None) -> ChangeHint:
    try:
        return ChangeHint(system=system, event_type=event_type, received_utc=received, record_id=record_id,
                          resource=resource, attributes=attributes or {})
    except ValidationError:  # never surfaced or logged: pydantic errors echo the offending input
        raise _bad_request() from None


def _log(vendor: str, outcome: str, status: int, size: int) -> None:
    level = logging.INFO if status < 400 else logging.WARNING if status < 500 else logging.ERROR
    _LOG.log(level, "webhook vendor=%s outcome=%s status=%d bytes=%d", vendor, outcome, status, size)


class _Gateway:
    """Request pipeline plus the vendor-specific verification / extraction steps."""

    def __init__(self, publisher: HintPublisher, secrets: WebhookSecrets, max_body_bytes: int,
                 clock: Callable[[], datetime]) -> None:
        self._publisher = publisher
        self._secrets = secrets
        self._max_body_bytes = max_body_bytes
        self._clock = clock

    def routes(self) -> tuple[_Route, ...]:
        """One route per vendor; Graph has two (change notifications and lifecycle notifications)."""
        return (
            _Route("/webhooks/ado", "ado", self._ado_precheck, self._extract_ado),
            _Route("/webhooks/servicenow", "servicenow", self._servicenow_precheck, self._extract_servicenow),
            _Route("/webhooks/pagerduty", "pagerduty", self._pagerduty_precheck, self._extract_pagerduty),
            _Route("/webhooks/msgraph", "msgraph", self._msgraph_precheck, self._extract_msgraph,
                   graph_handshake=True),
            _Route("/webhooks/msgraph/lifecycle", "msgraph", self._msgraph_precheck, self._extract_msgraph_lifecycle,
                   graph_handshake=True),
            # Figma counts anything but 200 OK as a failed delivery and retries it [K].
            _Route("/webhooks/figma", "figma", self._figma_precheck, self._extract_figma, success_status=200),
        )

    def endpoint(self, route: _Route) -> Callable[[Request], Awaitable[Response]]:
        """The ASGI endpoint for *route*."""
        async def webhook(request: Request) -> Response:
            return await self.handle(request, route)
        return webhook

    async def handle(self, request: Request, route: _Route) -> Response:
        """Run the pipeline for one delivery; every failure maps to a fixed, detail-free response."""
        received = self._clock()
        size = 0
        try:
            size = self._declared_length(request.headers)
            if route.graph_handshake and "validationToken" in request.query_params:
                return self._handshake(route.vendor, request.query_params["validationToken"])
            if not route.precheck(request.headers):
                raise _unauthorized()
            raw = await self._read_body(request)
            size = len(raw)
            hints = route.extract(raw, request.headers, received)
            if hints is None:
                _log(route.vendor, "ping", 200, size)
                return JSONResponse({"accepted": 0}, status_code=200)
            await self._publish(hints)
            _log(route.vendor, "accepted", route.success_status, size)
            return JSONResponse({"accepted": len(hints)}, status_code=route.success_status)
        except _Rejected as rejected:
            _log(route.vendor, rejected.outcome, rejected.status_code,
                 size if rejected.observed_bytes is None else rejected.observed_bytes)
            return JSONResponse({"error": _ERROR_CODES[rejected.status_code]}, status_code=rejected.status_code)

    # ------------------------------------------------------------------------------------------- shared plumbing
    def _declared_length(self, headers: Headers) -> int:
        declared = headers.get("content-length")
        if declared is None:
            return 0
        if not _CONTENT_LENGTH_RE.fullmatch(declared.strip()):
            raise _bad_request("bad_content_length")
        length = int(declared)
        if length > self._max_body_bytes:
            raise _Rejected(413, "too_large", observed_bytes=length)
        return length

    async def _read_body(self, request: Request) -> bytes:
        chunks: list[bytes] = []
        total = 0
        try:
            async with aclosing(request.stream()) as stream:
                async for chunk in stream:
                    total += len(chunk)
                    if total > self._max_body_bytes:
                        raise _Rejected(413, "too_large", observed_bytes=total)
                    chunks.append(chunk)
        except ClientDisconnect:
            raise _Rejected(400, "client_disconnected", observed_bytes=total) from None
        return b"".join(chunks)

    @staticmethod
    def _handshake(vendor: HintSystem, token: str) -> Response:
        # Graph subscription validation: echo the URL-decoded token as text/plain within 10 s; publish nothing.
        if not token or len(token) > MAX_VALIDATION_TOKEN_CHARS:
            raise _bad_request("bad_validation_token")
        _log(vendor, "handshake", 200, 0)
        return PlainTextResponse(token, headers={"X-Content-Type-Options": "nosniff", "Cache-Control": "no-store"})

    async def _publish(self, hints: list[ChangeHint]) -> None:
        for hint in hints:
            try:
                await run_in_threadpool(self._publisher.publish, hint)
            except Exception as exc:  # any publisher failure → 503 so the vendor redelivers later
                raise _Rejected(503, f"publish_failed:{type(exc).__name__}") from None

    # ---------------------------------------------------------------------------------------------- prechecks
    def _ado_precheck(self, headers: Headers) -> bool:
        # ADO authenticates with headers only, so the full check runs before the body is read.
        return verify_ado(headers.get("authorization"), headers.get(ADO_SHARED_SECRET_HEADER), self._secrets)

    def _servicenow_precheck(self, headers: Headers) -> bool:
        return SERVICENOW_SIGNATURE_HEADER in headers and "servicenow" not in self._secrets.unconfigured_vendors()

    def _pagerduty_precheck(self, headers: Headers) -> bool:
        return PAGERDUTY_SIGNATURE_HEADER in headers and "pagerduty" not in self._secrets.unconfigured_vendors()

    def _msgraph_precheck(self, _headers: Headers) -> bool:
        return is_configured(self._secrets.msgraph_client_state)

    def _figma_precheck(self, _headers: Headers) -> bool:
        return is_configured(self._secrets.figma_passcode)

    # --------------------------------------------------------------------------------------------- extractors
    def _extract_ado(self, raw: bytes, headers: Headers, received: datetime) -> list[ChangeHint] | None:
        if not self._ado_precheck(headers):
            raise _unauthorized()
        payload = _parse_object(raw)
        resource = _object(payload, "resource")
        # workitem.updated: resource is the update (id = update id, workItemId = the item); created/commented: the item.
        work_item = resource.get("workItemId")
        record_id = _identifier(resource.get("id") if work_item is None else work_item)
        attributes = {} if resource.get("rev") is None else {"rev": _identifier(resource.get("rev"))}
        return [_hint("ado", _identifier(payload.get("eventType")), received, record_id=record_id,
                      attributes=attributes)]

    def _extract_servicenow(self, raw: bytes, headers: Headers, received: datetime) -> list[ChangeHint] | None:
        if not verify_servicenow(headers.get(SERVICENOW_SIGNATURE_HEADER), raw, self._secrets):
            raise _unauthorized()
        payload = _parse_object(raw)
        table, sys_id = payload.get("table"), payload.get("sys_id")
        if not isinstance(table, str) or not _SERVICENOW_TABLE_RE.fullmatch(table):
            raise _bad_request()
        if not isinstance(sys_id, str) or not _SERVICENOW_SYS_ID_RE.fullmatch(sys_id):
            raise _bad_request()
        return [_hint("servicenow", _identifier(payload.get("event_type")), received, record_id=sys_id,
                      attributes={"table": table})]

    def _extract_pagerduty(self, raw: bytes, headers: Headers, received: datetime) -> list[ChangeHint] | None:
        if not verify_pagerduty(headers.get(PAGERDUTY_SIGNATURE_HEADER), raw, self._secrets):
            raise _unauthorized()
        event = _object(_parse_object(raw), "event")
        event_type = _identifier(event.get("event_type"))
        if event_type == _PAGERDUTY_PING:
            return None
        data = _object(event, "data")
        # Sub-resource events (incident.annotated notes, responders, status updates, ...) carry the sub-resource as
        # ``data`` (resource_type may still be "incident") and the parent as ``data.incident``; incident lifecycle
        # events carry the incident itself as ``data``. Either way the hint names the incident.
        incident = data.get("incident")
        parent_id = incident.get("id") if isinstance(incident, dict) else None
        record_id = _identifier(data.get("id") if parent_id is None else parent_id)
        attributes = {"event_id": _identifier(event.get("id")),
                      "resource_type": _identifier(event.get("resource_type"))}
        return [_hint("pagerduty", event_type, received, record_id=record_id, attributes=attributes)]

    def _extract_msgraph(self, raw: bytes, _headers: Headers, received: datetime) -> list[ChangeHint] | None:
        return self._graph_hints(raw, received, lifecycle=False)

    def _extract_msgraph_lifecycle(self, raw: bytes, _headers: Headers, received: datetime) -> list[ChangeHint] | None:
        return self._graph_hints(raw, received, lifecycle=True)

    def _graph_hints(self, raw: bytes, received: datetime, *, lifecycle: bool) -> list[ChangeHint]:
        # Items whose clientState does not verify are dropped; a verified item with bad identifiers fails the whole
        # delivery (400) so nothing is published partially.
        if not is_configured(self._secrets.msgraph_client_state):
            raise _unauthorized()
        items = _parse_object(raw).get("value")
        if not isinstance(items, list):
            raise _bad_request()
        verified = [item for item in items
                    if isinstance(item, dict) and verify_msgraph_client_state(item.get("clientState"), self._secrets)]
        if not verified:
            raise _unauthorized()
        hints: list[ChangeHint] = []
        for item in verified:
            subscription_id = _identifier(item.get("subscriptionId"))
            event_type = _identifier(item.get("lifecycleEvent" if lifecycle else "changeType"))
            resource = subscription_id if lifecycle else _identifier(item.get("resource"))
            hints.append(_hint("msgraph", event_type, received, resource=resource,
                               attributes={"subscription_id": subscription_id}))
        return hints

    def _extract_figma(self, raw: bytes, _headers: Headers, received: datetime) -> list[ChangeHint] | None:
        if not is_configured(self._secrets.figma_passcode):
            raise _unauthorized()
        payload = _parse_object(raw)
        if not verify_figma_passcode(payload.get("passcode"), self._secrets):
            raise _unauthorized()
        event_type = _identifier(payload.get("event_type"))
        if event_type == _FIGMA_PING:
            return None
        return [_hint("figma", event_type, received, record_id=_identifier(payload.get("file_key")),
                      attributes={"webhook_id": _identifier(payload.get("webhook_id"))})]


async def _healthz() -> JSONResponse:
    return JSONResponse({"status": "ok", "component": COMPONENT})


def create_app(settings: Settings, publisher: HintPublisher, secrets: WebhookSecrets, *,
               max_body_bytes: int = MAX_BODY_BYTES, clock: Callable[[], datetime] = utcnow) -> FastAPI:
    """Build the signal-event-gateway ASGI app (serve with uvicorn on ``$PORT``; GKE probes ``GET /healthz``).

    *publisher* receives identifier-only ``ChangeHint`` objects (``PubSubHintPublisher.from_settings(settings)`` on
    GKE, ``InMemoryHintPublisher`` in tests); *secrets* normally comes from ``WebhookSecrets.from_env()``. Vendors
    whose secrets are still placeholders are named once in a startup warning, and every request from them is
    rejected (fail closed). API docs routes are disabled: this is a public ingress.
    """
    if max_body_bytes <= 0:
        raise ValueError("max_body_bytes must be positive")
    gateway = _Gateway(publisher, secrets, max_body_bytes, clock)
    app = FastAPI(title=COMPONENT, version=settings.config_version, docs_url=None, redoc_url=None, openapi_url=None)
    app.add_api_route("/healthz", _healthz, methods=["GET"], include_in_schema=False)
    for route in gateway.routes():
        app.add_api_route(route.path, gateway.endpoint(route), methods=["POST"], include_in_schema=False,
                          name=route.path.removeprefix("/webhooks/").replace("/", "_") + "_webhook")
    unconfigured = secrets.unconfigured_vendors()
    if unconfigured:
        _LOG.warning("%s (%s): webhook verification not configured for %s; their requests will be rejected",
                     COMPONENT, settings.environment.value, ",".join(unconfigured))
    return app
