"""HTTP plumbing shared by the vendor clients (docs/09 §1: typed errors, ``Retry-After``, no silent successes).

* ``HttpRequest`` — what the pure request builders return (unit-testable without a network).
* ``Transport`` — sends a request and returns decoded JSON or raises ``ConnectorError``.
* ``UnconfiguredTransport`` — the default for every ``Http*Connector`` until SLB supplies credentials and sandbox
  endpoints: it fails loudly with the blocking open question (``not_implemented("Q-xx", …)``).
* ``HttpxTransport`` — the real client path (status → typed error mapping); credentials come from a callable
  (Secret Manager / Agent Identity Auth Manager — provider wiring is UNCONFIRMED, docs/08 §4).
Response bodies are never logged or copied into error messages (they may contain PII).
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any, Protocol
from urllib.parse import urlsplit

import httpx

from pm_agent.connectors.base import ConnectorError
from pm_agent.placeholders import not_implemented

Credentials = Callable[[], Mapping[str, str]]


@dataclass(frozen=True)
class HttpRequest:
    method: str
    url: str
    params: tuple[tuple[str, str], ...] = ()
    headers: Mapping[str, str] = field(default_factory=dict)
    json_body: Any = None

    @property
    def path(self) -> str:
        return urlsplit(self.url).path


class Transport(Protocol):
    def send(self, request: HttpRequest) -> Any: ...


class UnconfiguredTransport:
    """Placeholder transport: every call names the open question that blocks real connectivity."""

    def __init__(self, question_id: str, system: str) -> None:
        self.question_id = question_id
        self.system = system

    def send(self, request: HttpRequest) -> Any:
        """Always raises ``PlaceholderNotImplemented`` naming the open question (typed ``Any`` like ``Transport``)."""
        not_implemented(self.question_id, f"{self.system} REST transport and credentials ({request.method} "
                                          f"{request.path})")


def _header(headers: Mapping[str, str], name: str) -> str | None:
    wanted = name.lower()
    return next((value for key, value in headers.items() if key.lower() == wanted), None)


def retry_after_seconds(value: str | None, now: datetime | None = None) -> float | None:
    """``Retry-After`` as seconds (delta-seconds or HTTP-date)."""
    if value is None or not value.strip():
        return None
    text = value.strip()
    try:
        return max(float(text), 0.0)
    except ValueError:
        pass
    try:
        when = parsedate_to_datetime(text)
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return max((when - (now or datetime.now(UTC))).total_seconds(), 0.0)


def error_for_status(status: int, headers: Mapping[str, str]) -> ConnectorError:
    """Vendor HTTP status → typed connector error (429/503 carry ``Retry-After``)."""
    retry = retry_after_seconds(_header(headers, "Retry-After")) if status in (429, 503) else None
    if status in (401, 403):
        return ConnectorError("auth_failed", f"HTTP {status}")
    if status in (404, 410):
        return ConnectorError("not_found", f"HTTP {status}")
    if status == 429:
        return ConnectorError("rate_limited", "HTTP 429", retry_after_seconds=retry)
    if status >= 500:
        return ConnectorError("unavailable", f"HTTP {status}", retry_after_seconds=retry)
    return ConnectorError("validation_failed", f"HTTP {status}")


class HttpxTransport:
    """Real HTTP transport over an ``httpx.Client`` (inject ``httpx.MockTransport`` in tests)."""

    def __init__(self, client: httpx.Client, credentials: Credentials | None = None, timeout_seconds: float = 30.0
                 ) -> None:
        self._client = client
        self._credentials = credentials
        self._timeout = timeout_seconds

    def send(self, request: HttpRequest) -> Any:
        """One request: credentials applied, status mapped to a typed error, JSON decoded (``{}`` if empty)."""
        headers = dict(request.headers)
        if self._credentials is not None:
            headers.update(self._credentials())
        content = None
        if request.json_body is not None:
            headers.setdefault("Content-Type", "application/json")
            content = json.dumps(request.json_body).encode("utf-8")
        try:
            response = self._client.request(request.method, request.url, params=list(request.params),
                                            headers=headers, content=content, timeout=self._timeout)
        except httpx.HTTPError as exc:
            raise ConnectorError("unavailable", type(exc).__name__) from exc
        if response.status_code >= 400:
            raise error_for_status(response.status_code, response.headers)
        if not response.content:
            return {}
        try:
            return response.json()
        except ValueError as exc:
            raise ConnectorError("unavailable", "response is not JSON") from exc
