"""HTTP plumbing shared by the vendor clients (docs/09 §1: typed errors, ``Retry-After``, no silent successes).

* ``HttpRequest`` — what the pure request builders return (unit-testable without a network).
* ``Transport`` — sends a request and returns the decoded JSON object or raises ``ConnectorError``.
* ``UnconfiguredTransport`` — the default for every ``Http*Connector`` until SLB supplies credentials and sandbox
  endpoints: it fails loudly with the blocking open question (``not_implemented("Q-xx", …)``).
* ``HttpxTransport`` — the real client path (status → typed error mapping); credentials come from a callable
  (Secret Manager / Agent Identity Auth Manager — provider wiring is UNCONFIRMED, docs/08 §4).

Only an answer the API actually gave counts as success (IR3: never an empty success): ``200``/``201``/``202`` with a
JSON *object* body — every vendor API in scope answers with objects (docs/09 §2–§7) — or ``204 No Content`` (``{}``).
Anything that merely *looks* successful is a typed error, because the connectors read a missing ``value`` as an empty
page, i.e. "caught up": ``203`` (ADO's HTML sign-in page for a missing or expired credential) and ``3xx`` (httpx does
not follow redirects; for these APIs a redirect means a sign-in page) are ``auth_failed``; an empty, non-JSON or
non-object success body and any other ``2xx`` are ``unavailable``. ``Accept: application/json`` is sent unless the
request names its own media type (PagerDuty's vendor type). Response bodies are never logged or copied into error
messages (they may contain PII).
"""

from __future__ import annotations

import json
import re
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
SUCCESS_STATUSES = frozenset({200, 201, 202})
NO_CONTENT = 204
NOT_AUTHORITATIVE = 203
# RFC 9110 §10.2.3: ``delay-seconds = 1*DIGIT``. Ten digits (~317 years) is far beyond any real answer and keeps the
# value finite; fractions, signs, exponents, ``nan``/``inf`` and non-ASCII digits are not delay-seconds.
_DELAY_SECONDS = re.compile(r"[0-9]{1,10}")


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
    """``Retry-After`` (RFC 9110 §10.2.3) as non-negative, finite seconds; ``None`` when absent or unparseable.

    Accepts ``delay-seconds`` (ASCII digits only) and an HTTP-date — the IMF-fixdate plus the obsolete RFC 850 and
    asctime forms recipients must accept (RFC 9110 §5.6.7); a date in the past means "retry now" (``0.0``).
    """
    if value is None or not value.strip():
        return None
    text = value.strip()
    if _DELAY_SECONDS.fullmatch(text):
        return float(int(text))
    try:
        when = parsedate_to_datetime(text)
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None:  # ``-0000`` / asctime carry no zone; HTTP-dates are always GMT
        when = when.replace(tzinfo=UTC)
    return max((when - (now or datetime.now(UTC))).total_seconds(), 0.0)


def error_for_status(status: int, headers: Mapping[str, str]) -> ConnectorError:
    """A non-success vendor HTTP status → typed connector error (429/503 carry ``Retry-After``).

    ``203`` and ``3xx`` are sign-in pages, not API answers (see the module docstring), hence ``auth_failed``.
    """
    retry = retry_after_seconds(_header(headers, "Retry-After")) if status in (429, 503) else None
    if status == NOT_AUTHORITATIVE or 300 <= status < 400:
        return ConnectorError("auth_failed", f"HTTP {status} (sign-in page or redirect, not an API answer)")
    if status in (401, 403):
        return ConnectorError("auth_failed", f"HTTP {status}")
    if status in (404, 410):
        return ConnectorError("not_found", f"HTTP {status}")
    if status == 429:
        return ConnectorError("rate_limited", "HTTP 429", retry_after_seconds=retry)
    if status >= 500:
        return ConnectorError("unavailable", f"HTTP {status}", retry_after_seconds=retry)
    if status >= 400:
        return ConnectorError("validation_failed", f"HTTP {status}")
    return ConnectorError("unavailable", f"unexpected HTTP {status}")


def _json_object(response: httpx.Response) -> dict[str, Any]:
    """The success body as a JSON object; an empty, non-JSON or non-object body is a typed error (never ``{}``)."""
    if not response.content:
        raise ConnectorError("unavailable", f"HTTP {response.status_code} with an empty body")
    try:
        body = response.json()
    except ValueError as exc:  # JSONDecodeError and UnicodeDecodeError are both ValueErrors
        raise ConnectorError("unavailable", "response is not JSON") from exc
    if not isinstance(body, dict):
        raise ConnectorError("unavailable", "response is not a JSON object")
    return body


class HttpxTransport:
    """Real HTTP transport over an ``httpx.Client`` (inject ``httpx.MockTransport`` in tests)."""

    def __init__(self, client: httpx.Client, credentials: Credentials | None = None, timeout_seconds: float = 30.0
                 ) -> None:
        self._client = client
        self._credentials = credentials
        self._timeout = timeout_seconds

    def send(self, request: HttpRequest) -> Any:
        """One request: credentials applied, status mapped to a typed error, the JSON object body decoded."""
        headers = dict(request.headers)
        if self._credentials is not None:
            headers.update(self._credentials())
        if _header(headers, "Accept") is None:
            headers["Accept"] = "application/json"
        content = None
        if request.json_body is not None:
            if _header(headers, "Content-Type") is None:
                headers["Content-Type"] = "application/json"
            content = json.dumps(request.json_body).encode("utf-8")
        try:
            response = self._client.request(request.method, request.url, params=list(request.params),
                                            headers=headers, content=content, timeout=self._timeout)
        except httpx.HTTPError as exc:
            raise ConnectorError("unavailable", type(exc).__name__) from exc
        if response.status_code == NO_CONTENT:
            return {}
        if response.status_code not in SUCCESS_STATUSES:
            raise error_for_status(response.status_code, response.headers)
        return _json_object(response)
