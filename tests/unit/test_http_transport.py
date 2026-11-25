"""Vendor HTTP transport (docs/09 §1): typed errors, ``Retry-After``, never an empty success (IR3).

Exercised over ``httpx.MockTransport`` — no network. The failure modes that matter in production are the ones that
used to *look* like success: ADO answers a missing or expired credential with ``203`` and an HTML sign-in page, httpx
does not follow redirects (a ``302`` to a login page has no body), and an empty ``200`` body decoded to ``{}``. Each of
those became an empty page in the connectors (``.get("value") or []``), i.e. "caught up" — silently skipping data.
"""
from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime
from typing import Any

import httpx
import pytest

from pm_agent.connectors.base import ConnectorError
from pm_agent.connectors.http import HttpRequest, HttpxTransport, error_for_status, retry_after_seconds

URL = "https://api.example.invalid/v1/items"
SIGN_IN_PAGE = b"<html><body>Sign in to continue, jane.doe@example.com</body></html>"
NOW = datetime(2026, 11, 24, 12, 0, tzinfo=UTC)
Handler = Callable[[httpx.Request], httpx.Response]


class Recorder:
    """``httpx.MockTransport`` handler: answers every request with *respond* and keeps what it was sent."""

    def __init__(self, respond: Handler) -> None:
        self.requests: list[httpx.Request] = []
        self._respond = respond

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self._respond(request)


def _transport(respond: Handler, credentials: Callable[[], Mapping[str, str]] | None = None
               ) -> tuple[HttpxTransport, Recorder]:
    recorder = Recorder(respond)
    return HttpxTransport(httpx.Client(transport=httpx.MockTransport(recorder)), credentials=credentials), recorder


def _answer(status: int, content: bytes = b"", headers: Mapping[str, str] | None = None) -> Handler:
    return lambda request: httpx.Response(status, content=content, headers=dict(headers or {}))


def _send(transport: HttpxTransport, **kwargs: Any) -> Any:
    return transport.send(HttpRequest(method=kwargs.pop("method", "GET"), url=URL, **kwargs))


def _error(transport: HttpxTransport, **kwargs: Any) -> ConnectorError:
    with pytest.raises(ConnectorError) as excinfo:
        _send(transport, **kwargs)
    return excinfo.value


# ----------------------------------------------------------------------------------------------- successes
@pytest.mark.parametrize("status", [200, 201, 202])
def test_json_object_bodies_are_returned(status: int) -> None:
    transport, _ = _transport(lambda request: httpx.Response(status, json={"value": [{"id": 1}]}))
    assert _send(transport) == {"value": [{"id": 1}]}


def test_204_no_content_is_an_empty_object() -> None:
    transport, _ = _transport(_answer(204))
    assert _send(transport) == {}


# ------------------------------------------------------------------------------------- never "caught up"
@pytest.mark.parametrize("status", [200, 201, 202])
def test_empty_success_body_is_a_typed_error_not_an_empty_page(status: int) -> None:
    transport, _ = _transport(_answer(status))
    assert _error(transport).code == "unavailable"


def test_non_json_success_body_is_a_typed_error_that_never_echoes_the_body() -> None:
    transport, _ = _transport(_answer(200, SIGN_IN_PAGE, {"Content-Type": "text/html"}))
    error = _error(transport)
    assert error.code == "unavailable"
    assert "jane.doe" not in str(error) and "Sign in" not in str(error)  # bodies may carry PII


@pytest.mark.parametrize("body", [b"[]", b"null", b"42", b'"done"', b"true"])
def test_success_body_must_be_a_json_object(body: bytes) -> None:
    transport, _ = _transport(_answer(200, body, {"Content-Type": "application/json"}))
    assert _error(transport).code == "unavailable"


def test_ado_203_sign_in_page_is_auth_failed() -> None:
    transport, _ = _transport(_answer(203, SIGN_IN_PAGE, {"Content-Type": "text/html; charset=utf-8"}))
    error = _error(transport)
    assert error.code == "auth_failed" and "203" in str(error)
    assert "jane.doe" not in str(error)


@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
def test_redirects_are_auth_failed_and_never_followed(status: int) -> None:
    transport, recorder = _transport(_answer(status, headers={"Location": "https://login.example.invalid/signin"}))
    assert _error(transport).code == "auth_failed"
    assert [r.url.host for r in recorder.requests] == ["api.example.invalid"]  # one request, no hop to the login page


@pytest.mark.parametrize("status", [205, 206, 207])
def test_unexpected_2xx_statuses_are_typed_errors(status: int) -> None:
    transport, _ = _transport(lambda request: httpx.Response(status, json={"value": []}))
    assert _error(transport).code == "unavailable"


@pytest.mark.parametrize(("status", "code"), [(400, "validation_failed"), (401, "auth_failed"), (403, "auth_failed"),
                                              (404, "not_found"), (409, "validation_failed"), (410, "not_found"),
                                              (422, "validation_failed"), (429, "rate_limited"),
                                              (500, "unavailable"), (502, "unavailable"), (503, "unavailable")])
def test_error_statuses_map_to_typed_codes(status: int, code: str) -> None:
    transport, _ = _transport(_answer(status, b'{"message": "secret detail"}', {"Retry-After": "11"}))
    error = _error(transport)
    assert error.code == code and "secret detail" not in str(error)
    assert error.retry_after_seconds == (11.0 if status in (429, 503) else None)


@pytest.mark.parametrize("status", [203, 300, 301, 302, 304, 307, 308])
def test_error_for_status_treats_203_and_3xx_as_auth_failures(status: int) -> None:
    assert error_for_status(status, {}).code == "auth_failed"


def test_transport_failures_are_unavailable() -> None:
    def timeout(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    transport, _ = _transport(timeout)
    error = _error(transport)
    assert error.code == "unavailable" and "ReadTimeout" in str(error)


# --------------------------------------------------------------------------------------------- headers
def test_accept_json_is_sent_by_default() -> None:
    transport, recorder = _transport(lambda request: httpx.Response(200, json={}))
    _send(transport)
    assert recorder.requests[0].headers.get_list("Accept") == ["application/json"]


def test_vendor_accept_header_wins_case_insensitively() -> None:
    transport, recorder = _transport(lambda request: httpx.Response(200, json={}))
    _send(transport, headers={"accept": "application/vnd.pagerduty+json;version=2"})
    assert recorder.requests[0].headers.get_list("Accept") == ["application/vnd.pagerduty+json;version=2"]


def test_json_body_is_serialized_with_exactly_one_content_type() -> None:
    transport, recorder = _transport(lambda request: httpx.Response(201, json={"id": 7}))
    assert _send(transport, method="POST", json_body={"query": "x"}) == {"id": 7}
    assert _send(transport, method="PATCH", json_body=[{"op": "test", "path": "/rev", "value": 3}],
                 headers={"content-type": "application/json-patch+json"}) == {"id": 7}
    first, second = recorder.requests
    assert first.headers.get_list("Content-Type") == ["application/json"] and json.loads(first.content) == {"query": "x"}
    assert second.headers.get_list("Content-Type") == ["application/json-patch+json"]
    assert json.loads(second.content) == [{"op": "test", "path": "/rev", "value": 3}]


def test_credentials_and_query_parameters_are_applied() -> None:
    transport, recorder = _transport(lambda request: httpx.Response(200, json={"ok": True}),
                                     credentials=lambda: {"Authorization": "Bearer test-only"})
    assert _send(transport, params=(("api-version", "7.1"), ("ids", "1")), headers={"Authorization": "stale"}) == \
        {"ok": True}
    sent = recorder.requests[0]
    assert sent.headers.get_list("Authorization") == ["Bearer test-only"]
    assert dict(sent.url.params) == {"api-version": "7.1", "ids": "1"}


# ------------------------------------------------------------------------------------------ Retry-After
@pytest.mark.parametrize(("value", "seconds"), [("120", 120.0), (" 7 ", 7.0), ("0", 0.0)])
def test_retry_after_delay_seconds(value: str, seconds: float) -> None:
    assert retry_after_seconds(value, NOW) == seconds


@pytest.mark.parametrize("value", ["Tue, 24 Nov 2026 12:02:00 GMT",  # IMF-fixdate (RFC 9110 §5.6.7)
                                   "Tuesday, 24-Nov-26 12:02:00 GMT",  # obsolete RFC 850 form
                                   "Tue Nov 24 12:02:00 2026"])  # obsolete asctime form
def test_retry_after_http_date_forms(value: str) -> None:
    assert retry_after_seconds(value, NOW) == 120.0


def test_retry_after_in_the_past_means_now() -> None:
    assert retry_after_seconds("Tue, 24 Nov 2026 11:00:00 GMT", NOW) == 0.0


@pytest.mark.parametrize("value", [None, "", "   ", "nan", "inf", "-inf", "1e400", "-5", "1.5", "1e3", "1_000",
                                   "\u0663", "0x10", "99999999999", "garbage", "Tue, 32 Nov 2026 12:02:00 GMT"])
def test_retry_after_rejects_values_that_are_neither_delay_seconds_nor_dates(value: str | None) -> None:
    assert retry_after_seconds(value, NOW) is None


def test_rate_limit_error_honours_an_http_date_retry_after() -> None:
    later = format_datetime(datetime.now(UTC) + timedelta(hours=1), usegmt=True)
    transport, _ = _transport(_answer(429, headers={"Retry-After": later}))
    error = _error(transport)
    assert error.code == "rate_limited"
    assert error.retry_after_seconds is not None and 3500 < error.retry_after_seconds <= 3600


def test_http_request_path_is_the_url_path() -> None:
    assert HttpRequest(method="GET", url=URL + "?a=1").path == "/v1/items"
