"""PagerDuty connector (``mcp-pagerduty``) — docs/09 §4.

* Normalizers: incident → ``incident`` signal (``source_record_id`` = incident number, matching ``PD #<n>``
  cross-references; ``source_version`` = ``updated_at``; permalink = the incident ``html_url``); notes and log entries
  → child ``journal_note`` signals.
* ``PagerDutyApi``: REST v2 ``/incidents`` (``since``/``until``, ``statuses[]``, ``service_ids[]``, ``limit ≤ 100``),
  ``/incidents/{id}/notes`` and ``/incidents/{id}/log_entries`` [K].
* ``FixturePagerDutyConnector`` and ``HttpPagerDutyConnector`` (API key vs OAuth app and services in scope: Q-07).

Open item: ``PLACEHOLDER:Q-07:pagerduty_api_credentials`` (read-only REST key or scoped OAuth app).
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any
from urllib.parse import quote

from pm_agent.connectors.base import ConnectorError, PollResult, make_signal
from pm_agent.connectors.common import (
    as_mapping,
    FixtureConnectorBase,
    FixtureUnit,
    IngestContext,
    context_or,
    default_fixture_root,
    first_text,
    newest_timestamp,
    normalize_text,
    checked_limit,
    parse_utc,
    person_ref,
    require_timestamp,
)
from pm_agent.connectors.entities import EntityResolver, resolve_customer, resolve_product
from pm_agent.connectors.http import HttpRequest, Transport, UnconfiguredTransport
from pm_agent.grounding.redaction import Redactor
from pm_agent.placeholders import is_placeholder, require
from pm_agent.schemas import RecordType, Signal, SourceSystem
from pm_agent.settings import Settings

QUESTION = "Q-07"
CONNECTOR = "mcp-pagerduty"
MAX_LIMIT = 100
ACCEPT = "application/vnd.pagerduty+json;version=2"
_ID = re.compile(r"^[A-Za-z0-9]{1,40}$")


def pagerduty_permalink(web_base: str, incident_id: str) -> str:
    """``https://<subdomain>.pagerduty.com/incidents/<id>`` (the API's ``html_url`` is preferred when present)."""
    return f"{web_base.rstrip('/')}/incidents/{quote(incident_id, safe='')}"


def _check_id(value: str) -> str:
    if not _ID.match(value):
        raise ValueError(f"invalid PagerDuty id {value!r}")
    return value


def normalize_pagerduty_incident(raw: Mapping[str, Any], *, redactor: Redactor,
                                 entities: EntityResolver | None = None, ctx: IngestContext | None = None) -> Signal:
    """One incident → redacted ``incident`` signal keyed by its incident number."""
    record_id = first_text(raw.get("incident_number"), raw.get("id"))
    permalink = first_text(raw.get("html_url"))
    if not record_id or not permalink:
        raise ConnectorError("validation_failed", "incident without number/id or html_url")
    body = raw.get("body")
    details = first_text(body.get("details")) if isinstance(body, Mapping) else ""
    title = normalize_text(first_text(raw.get("title"), raw.get("summary"))) or None
    text = normalize_text(details or first_text(raw.get("description"))) or (title or "")
    version = first_text(raw.get("updated_at"), raw.get("last_status_change_at"), raw.get("created_at")) or "0"
    return make_signal(
        redactor=redactor, source_system=SourceSystem.PAGERDUTY, source_record_id=record_id, source_version=version,
        record_type=RecordType.INCIDENT, permalink=permalink, raw_text=text, title=title,
        captured_utc=parse_utc(version) or parse_utc(raw.get("created_at")),
        customer=resolve_customer(entities, None, title, text), product=resolve_product(entities, title, text),
        **context_or(ctx, CONNECTOR).stamp())


def normalize_pagerduty_note(raw: Mapping[str, Any], *, redactor: Redactor, parent: Signal,
                             ctx: IngestContext | None = None) -> Signal:
    """One incident note → child ``journal_note`` signal."""
    note_id = first_text(raw.get("id"))
    if not note_id:
        raise ConnectorError("validation_failed", "note without id")
    user = as_mapping(raw.get("user"))
    return make_signal(
        redactor=redactor, source_system=SourceSystem.PAGERDUTY, source_record_id=note_id, source_version="1",
        record_type=RecordType.JOURNAL_NOTE, permalink=f"{parent.permalink}#note-{quote(note_id, safe='')}",
        raw_text=normalize_text(first_text(raw.get("content"))), title="Incident note",
        captured_utc=parse_utc(raw.get("created_at")), author=person_ref(redactor, first_text(user.get("id")) or None),
        customer=parent.customer, product=parent.product, parent_signal_id=parent.signal_id,
        **context_or(ctx, CONNECTOR).stamp())


def normalize_pagerduty_log_entry(raw: Mapping[str, Any], *, redactor: Redactor, parent: Signal,
                                  ctx: IngestContext | None = None) -> Signal:
    """One incident log entry (trigger/acknowledge/resolve/…) → child ``journal_note`` signal."""
    entry_id = first_text(raw.get("id"))
    if not entry_id:
        raise ConnectorError("validation_failed", "log entry without id")
    agent = as_mapping(raw.get("agent"))
    is_user = first_text(agent.get("type")).startswith("user")
    return make_signal(
        redactor=redactor, source_system=SourceSystem.PAGERDUTY, source_record_id=entry_id, source_version="1",
        record_type=RecordType.JOURNAL_NOTE, permalink=f"{parent.permalink}#log-{quote(entry_id, safe='')}",
        raw_text=normalize_text(first_text(raw.get("summary"))), title=first_text(raw.get("type")) or None,
        captured_utc=parse_utc(raw.get("created_at")),
        author=person_ref(redactor, first_text(agent.get("id")) or None) if is_user else None,
        customer=parent.customer, product=parent.product, parent_signal_id=parent.signal_id,
        **context_or(ctx, CONNECTOR).stamp())


class PagerDutyApi:
    """Pure request builders for PagerDuty REST v2 [K]."""

    def __init__(self, api_base: str = "https://api.pagerduty.com") -> None:
        self._base = api_base.rstrip("/")
        self._headers = {"Accept": ACCEPT, "Content-Type": "application/json"}

    def incidents(self, since: str | None, until: str | None = None, *, statuses: Sequence[str] = (),
                  service_ids: Sequence[str] = (), limit: int = MAX_LIMIT, offset: int = 0) -> HttpRequest:
        """``GET /incidents`` with repeated ``statuses[]``/``service_ids[]`` keys; ``limit`` capped at 100."""
        params: list[tuple[str, str]] = []
        if since:
            params.append(("since", since))
        if until:
            params.append(("until", until))
        params += [("statuses[]", status) for status in statuses]
        params += [("service_ids[]", _check_id(service)) for service in service_ids]
        params += [("limit", str(max(1, min(limit, MAX_LIMIT)))), ("offset", str(max(0, offset))),
                   ("time_zone", "UTC")]
        return HttpRequest(method="GET", url=f"{self._base}/incidents", params=tuple(params), headers=self._headers)

    def incident(self, incident_id: str) -> HttpRequest:
        return HttpRequest(method="GET", url=f"{self._base}/incidents/{_check_id(incident_id)}", headers=self._headers)

    def notes(self, incident_id: str) -> HttpRequest:
        return HttpRequest(method="GET", url=f"{self._base}/incidents/{_check_id(incident_id)}/notes",
                           headers=self._headers)

    def log_entries(self, incident_id: str, *, limit: int = MAX_LIMIT, offset: int = 0) -> HttpRequest:
        return HttpRequest(method="GET", url=f"{self._base}/incidents/{_check_id(incident_id)}/log_entries",
                           params=(("limit", str(max(1, min(limit, MAX_LIMIT)))), ("offset", str(max(0, offset))),
                                   ("is_overview", "false")), headers=self._headers)


class FixturePagerDutyConnector(FixtureConnectorBase):
    """PagerDuty over synthetic REST v2 JSON (``tests/fixtures/connectors/pagerduty``)."""

    def __init__(self, redactor: Redactor, *, path: Path | None = None, entities: EntityResolver | None = None,
                 run_id: str = "fixture") -> None:
        data = json.loads((path or default_fixture_root() / "pagerduty" / "incidents.json").read_text(encoding="utf-8"))
        ctx = IngestContext(connector="fixture-pagerduty", run_id=run_id)
        self._notes: dict[str, tuple[Signal, ...]] = {}
        self._logs: dict[str, tuple[Signal, ...]] = {}
        units = []
        for raw in data["incidents"]["incidents"]:
            parent = normalize_pagerduty_incident(raw, redactor=redactor, entities=entities, ctx=ctx)
            api_id = first_text(raw.get("id"))
            notes = tuple(normalize_pagerduty_note(n, redactor=redactor, parent=parent, ctx=ctx)
                          for n in ((data.get("notes") or {}).get(api_id) or {}).get("notes") or [])
            logs = tuple(normalize_pagerduty_log_entry(e, redactor=redactor, parent=parent, ctx=ctx)
                         for e in ((data.get("log_entries") or {}).get(api_id) or {}).get("log_entries") or [])
            self._notes.setdefault(parent.source_record_id, notes)
            self._logs.setdefault(parent.source_record_id, logs)
            units.append(FixtureUnit(parent=parent, children=notes + logs, aliases=frozenset({api_id})))
        super().__init__(SourceSystem.PAGERDUTY, units)

    def _number(self, incident: str) -> str | None:
        signal = self.get(incident)
        return signal.source_record_id if signal is not None else None

    def notes(self, incident: str) -> list[Signal]:
        return list(self._notes.get(self._number(incident) or "", ()))

    def log_entries(self, incident: str) -> list[Signal]:
        return list(self._logs.get(self._number(incident) or "", ()))


class HttpPagerDutyConnector:
    """PagerDuty REST v2 client (read-only key or scoped OAuth app; account/services are Q-07)."""

    source = SourceSystem.PAGERDUTY

    def __init__(self, *, redactor: Redactor, api_base: str = "https://api.pagerduty.com",
                 service_ids: Sequence[str] = (), entities: EntityResolver | None = None,
                 transport: Transport | None = None, run_id: str = "adhoc") -> None:
        self._api = PagerDutyApi(api_base)
        self._service_ids = list(service_ids)
        self._redactor = redactor
        self._entities = entities
        self._transport = transport or UnconfiguredTransport("Q-07", "pagerduty")
        self._ctx = IngestContext(connector=CONNECTOR, run_id=run_id)

    @classmethod
    def from_settings(cls, settings: Settings, redactor: Redactor, *, transport: Transport | None = None,
                      entities: EntityResolver | None = None) -> "HttpPagerDutyConnector":
        cfg = settings.connectors.get("pagerduty") or {}
        services = require(cfg.get("service_ids"), strict=settings.strict)
        service_ids = [str(s) for s in services] if isinstance(services, list) else \
            ([] if services is None or is_placeholder(services) else [str(services)])
        return cls(redactor=redactor, api_base=str(cfg.get("api_base") or "https://api.pagerduty.com"),
                   service_ids=service_ids, entities=entities or EntityResolver.from_settings(settings),
                   transport=transport)

    def _normalize(self, raw: Mapping[str, Any]) -> Signal:
        return normalize_pagerduty_incident(raw, redactor=self._redactor, entities=self._entities, ctx=self._ctx)

    def _incidents(self, cursor: str | None, limit: int) -> tuple[list[dict[str, Any]], str | None]:
        since = require_timestamp(cursor) if cursor else None
        response = self._transport.send(self._api.incidents(since, service_ids=self._service_ids,
                                                            limit=checked_limit(limit)))
        incidents = [dict(i) for i in response.get("incidents") or []]
        # ``since`` filters on creation time [K]; updates to older incidents arrive through webhook hints + get().
        return incidents, newest_timestamp(i.get("created_at") for i in incidents) or cursor

    def poll_records(self, cursor: str | None, limit: int = 100) -> PollResult:
        incidents, next_cursor = self._incidents(cursor, limit)
        return PollResult(source=self.source, signals=[self._normalize(i) for i in incidents], next_cursor=next_cursor)

    def poll(self, cursor: str | None, limit: int = 100) -> PollResult:
        incidents, next_cursor = self._incidents(cursor, limit)
        signals: list[Signal] = []
        for raw in incidents:
            parent = self._normalize(raw)
            api_id = first_text(raw.get("id"))
            signals += [parent, *self._notes_for(parent, api_id), *self._logs_for(parent, api_id)]
        return PollResult(source=self.source, signals=signals, next_cursor=next_cursor)

    def _raw(self, incident_id: str) -> dict[str, Any] | None:
        try:
            response = self._transport.send(self._api.incident(incident_id))
        except ConnectorError as exc:
            if exc.code == "not_found":
                return None
            raise
        incident = response.get("incident")
        return dict(incident) if isinstance(incident, Mapping) else None

    def get(self, record_id: str) -> Signal | None:
        raw = self._raw(record_id) if _ID.match(record_id) else None
        return self._normalize(raw) if raw else None

    def _notes_for(self, parent: Signal, api_id: str) -> list[Signal]:
        response = self._transport.send(self._api.notes(api_id))
        return [normalize_pagerduty_note(n, redactor=self._redactor, parent=parent, ctx=self._ctx)
                for n in response.get("notes") or []]

    def _logs_for(self, parent: Signal, api_id: str) -> list[Signal]:
        response = self._transport.send(self._api.log_entries(api_id))
        return [normalize_pagerduty_log_entry(e, redactor=self._redactor, parent=parent, ctx=self._ctx)
                for e in response.get("log_entries") or []]

    def notes(self, incident: str) -> list[Signal]:
        raw = self._raw(incident) if _ID.match(incident) else None
        return self._notes_for(self._normalize(raw), first_text(raw.get("id"))) if raw else []

    def log_entries(self, incident: str) -> list[Signal]:
        raw = self._raw(incident) if _ID.match(incident) else None
        return self._logs_for(self._normalize(raw), first_text(raw.get("id"))) if raw else []
