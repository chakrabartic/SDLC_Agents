"""ServiceNow connector (``mcp-servicenow``) — docs/09 §3.

* Normalizers: Table API row (``sysparm_display_value=all`` objects or plain values) → ``incident`` / ``request`` /
  ``case`` signal (``source_record_id`` = number, ``source_version`` = ``sys_mod_count``, permalink
  ``{instance}/{table}.do?sys_id=…``); ``sys_journal_field`` entry → child ``journal_note`` signal.
* ``ServiceNowApi``: Table API delta (``sys_updated_on>=cursor^ORDERBYsys_updated_on``) and journal reads. Every value
  that enters an encoded query is validated (tables, sys_ids, numbers, timestamps) — no query injection.
* ``FixtureServiceNowConnector`` and ``HttpServiceNowConnector`` (OAuth integration user / instance are Q-05).

Open item: ``PLACEHOLDER:Q-05:servicenow_oauth_client`` (read-only integration user, Application Registry).
"""

from __future__ import annotations

import base64
import binascii
import json
import re
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any
from urllib.parse import quote

from pm_agent.connectors.base import ConnectorError, PollResult, make_signal
from pm_agent.connectors.common import (
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
)
from pm_agent.connectors.entities import EntityResolver, resolve_customer, resolve_product
from pm_agent.connectors.http import HttpRequest, Transport, UnconfiguredTransport
from pm_agent.grounding.redaction import Redactor
from pm_agent.placeholders import is_placeholder, not_implemented, require
from pm_agent.schemas import RecordType, Signal, SourceSystem
from pm_agent.settings import Settings

QUESTION = "Q-05"
CONNECTOR = "mcp-servicenow"
RECORD_TYPES: dict[str, RecordType] = {
    "incident": RecordType.INCIDENT,
    "sc_request": RecordType.REQUEST,
    "sc_req_item": RecordType.REQUEST,
    "sn_customerservice_case": RecordType.CASE,
}
NUMBER_PREFIXES = {"INC": "incident", "REQ": "sc_request", "RITM": "sc_req_item", "CS": "sn_customerservice_case"}
JOURNAL_FIELDS = "sys_id,element,element_id,name,value,sys_created_on,sys_created_by"
_JOURNAL_TITLES = {"work_notes": "Work note", "comments": "Additional comment"}
_TABLE = re.compile(r"^[a-z0-9_]{1,80}$")
_SYS_ID = re.compile(r"^[0-9a-f]{32}$")
_NUMBER = re.compile(r"^(INC|REQ|RITM|CS)\d{7,10}$")
_SNOW_DATETIME = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$")
_CURSOR_PREFIX = "sn1:"


def _value(field: object) -> str:
    if isinstance(field, Mapping):
        return first_text(field.get("value"))
    return first_text(field)


def _display(field: object) -> str:
    if isinstance(field, Mapping):
        return first_text(field.get("display_value"), field.get("value"))
    return first_text(field)


def _check_table(table: str) -> str:
    if not _TABLE.match(table):
        raise ValueError(f"invalid table name {table!r}")
    return table


def _check_sys_id(sys_id: str) -> str:
    if not _SYS_ID.match(sys_id):
        raise ValueError("sys_id must be 32 lowercase hex characters")
    return sys_id


def servicenow_permalink(instance_url: str, table: str, sys_id: str) -> str:
    """Direct record URL ``{instance}/{table}.do?sys_id={sys_id}`` [K]."""
    return f"{instance_url.rstrip('/')}/{quote(table, safe='')}.do?sys_id={quote(sys_id, safe='')}"


def normalize_servicenow_record(raw: Mapping[str, Any], *, redactor: Redactor, instance_url: str,
                                table: str | None = None, entities: EntityResolver | None = None,
                                ctx: IngestContext | None = None) -> Signal:
    """One Table API row → redacted incident/request/case signal."""
    table_name = table or _value(raw.get("sys_class_name"))
    record_type = RECORD_TYPES.get(table_name)
    if record_type is None:
        raise ConnectorError("validation_failed", f"table {table_name!r} is not in the connector scope")
    sys_id = _value(raw.get("sys_id"))
    number = _value(raw.get("number"))
    if not sys_id or not number:
        raise ConnectorError("validation_failed", "record without sys_id/number")
    title = normalize_text(_value(raw.get("short_description"))) or None
    text = normalize_text(_value(raw.get("description")))
    close_notes = normalize_text(_value(raw.get("close_notes")))
    if close_notes:
        text = f"{text}\n\nResolution notes:\n{close_notes}".strip()
    structured_customer = _display(raw.get("account")) or _display(raw.get("company"))
    return make_signal(
        redactor=redactor, source_system=SourceSystem.SERVICENOW, source_record_id=number,
        source_version=_value(raw.get("sys_mod_count")) or "0", record_type=record_type,
        permalink=servicenow_permalink(instance_url, table_name, sys_id), raw_text=text or (title or ""), title=title,
        captured_utc=parse_utc(_value(raw.get("sys_updated_on"))) or parse_utc(_value(raw.get("sys_created_on"))),
        author=person_ref(redactor, _value(raw.get("opened_by")) or None),
        customer=resolve_customer(entities, structured_customer, title, text),
        product=resolve_product(entities, title, text), **context_or(ctx, CONNECTOR).stamp())


def normalize_servicenow_journal(raw: Mapping[str, Any], *, redactor: Redactor, parent: Signal,
                                 ctx: IngestContext | None = None) -> Signal:
    """One ``sys_journal_field`` entry (comments / work_notes) → child ``journal_note`` signal of *parent*."""
    sys_id = _value(raw.get("sys_id"))
    if not sys_id:
        raise ConnectorError("validation_failed", "journal entry without sys_id")
    element = _value(raw.get("element"))
    return make_signal(
        redactor=redactor, source_system=SourceSystem.SERVICENOW, source_record_id=sys_id, source_version="1",
        record_type=RecordType.JOURNAL_NOTE, permalink=f"{parent.permalink}#sys_journal_field={sys_id}",
        raw_text=normalize_text(_value(raw.get("value"))), title=_JOURNAL_TITLES.get(element),
        captured_utc=parse_utc(_value(raw.get("sys_created_on"))),
        author=person_ref(redactor, _value(raw.get("sys_created_by")) or None), customer=parent.customer,
        product=parent.product, parent_signal_id=parent.signal_id, **context_or(ctx, CONNECTOR).stamp())


class ServiceNowApi:
    """Pure request builders for the ServiceNow Table API [K]."""

    def __init__(self, instance_url: str) -> None:
        self._base = instance_url.rstrip("/")

    @staticmethod
    def delta_query(since: str | None) -> str:
        if since is not None and not _SNOW_DATETIME.match(since):
            raise ValueError("since must be 'YYYY-MM-DD HH:MM:SS' (UTC)")
        return (f"sys_updated_on>={since}^" if since else "") + "ORDERBYsys_updated_on"

    def table_delta(self, table: str, since: str | None, *, limit: int, offset: int = 0,
                    fields: Sequence[str] = ()) -> HttpRequest:
        params = [("sysparm_query", self.delta_query(since)), ("sysparm_display_value", "all"),
                  ("sysparm_exclude_reference_link", "true"), ("sysparm_limit", str(limit)),
                  ("sysparm_offset", str(offset))]
        if fields:
            params.append(("sysparm_fields", ",".join(fields)))
        return HttpRequest(method="GET", url=f"{self._base}/api/now/table/{_check_table(table)}",
                           params=tuple(params), headers={"Accept": "application/json"})

    def by_number(self, table: str, number: str) -> HttpRequest:
        if not _NUMBER.match(number):
            raise ValueError(f"invalid record number {number!r}")
        return HttpRequest(method="GET", url=f"{self._base}/api/now/table/{_check_table(table)}",
                           params=(("sysparm_query", f"number={number}"), ("sysparm_display_value", "all"),
                                   ("sysparm_exclude_reference_link", "true"), ("sysparm_limit", "1")),
                           headers={"Accept": "application/json"})

    def record(self, table: str, sys_id: str) -> HttpRequest:
        return HttpRequest(method="GET",
                           url=f"{self._base}/api/now/table/{_check_table(table)}/{_check_sys_id(sys_id)}",
                           params=(("sysparm_display_value", "all"), ("sysparm_exclude_reference_link", "true")),
                           headers={"Accept": "application/json"})

    def journal(self, sys_id: str, *, limit: int = 200) -> HttpRequest:
        query = f"element_id={_check_sys_id(sys_id)}^elementINcomments,work_notes^ORDERBYsys_created_on"
        return HttpRequest(method="GET", url=f"{self._base}/api/now/table/sys_journal_field",
                           params=(("sysparm_query", query), ("sysparm_fields", JOURNAL_FIELDS),
                                   ("sysparm_limit", str(limit))), headers={"Accept": "application/json"})


class FixtureServiceNowConnector(FixtureConnectorBase):
    """ServiceNow over synthetic Table API JSON (``tests/fixtures/connectors/servicenow``)."""

    def __init__(self, redactor: Redactor, *, path: Path | None = None, entities: EntityResolver | None = None,
                 run_id: str = "fixture") -> None:
        data = json.loads((path or default_fixture_root() / "servicenow" / "tables.json").read_text(encoding="utf-8"))
        instance = data["_meta"]["instance_url"]
        ctx = IngestContext(connector="fixture-servicenow", run_id=run_id)
        journal = list((data.get("sys_journal_field") or {}).get("result") or [])
        units = []
        for table, payload in (data.get("tables") or {}).items():
            for raw in payload.get("result") or []:
                parent = normalize_servicenow_record(raw, redactor=redactor, instance_url=instance, table=table,
                                                     entities=entities, ctx=ctx)
                sys_id = _value(raw.get("sys_id"))
                children = tuple(normalize_servicenow_journal(entry, redactor=redactor, parent=parent, ctx=ctx)
                                 for entry in journal if _value(entry.get("element_id")) == sys_id)
                units.append(FixtureUnit(parent=parent, children=children, aliases=frozenset({sys_id})))
        super().__init__(SourceSystem.SERVICENOW, units)

    def journal(self, record_id: str) -> list[Signal]:
        return self.children(record_id)


def _encode_cursor(watermarks: Mapping[str, str]) -> str:
    return _CURSOR_PREFIX + base64.urlsafe_b64encode(json.dumps(dict(watermarks), sort_keys=True).encode()).decode()


def _decode_cursor(cursor: str | None) -> dict[str, str]:
    if not cursor:
        return {}
    try:
        if not cursor.startswith(_CURSOR_PREFIX):
            raise ValueError("prefix")
        data = json.loads(base64.urlsafe_b64decode(cursor[len(_CURSOR_PREFIX):].encode()))
    except (ValueError, binascii.Error) as exc:
        raise ConnectorError("validation_failed", "unrecognized cursor") from exc
    if not isinstance(data, dict) or not all(isinstance(k, str) and _TABLE.match(k) and isinstance(v, str)
                                             and _SNOW_DATETIME.match(v) for k, v in data.items()):
        raise ConnectorError("validation_failed", "unrecognized cursor")
    return data


class HttpServiceNowConnector:
    """ServiceNow Table API client (OAuth read-only integration user; instance and scope are Q-05)."""

    source = SourceSystem.SERVICENOW

    def __init__(self, *, instance_url: str, redactor: Redactor, tables: Sequence[str] = tuple(RECORD_TYPES),
                 entities: EntityResolver | None = None, transport: Transport | None = None,
                 run_id: str = "adhoc") -> None:
        self._instance = instance_url
        self._api = ServiceNowApi(instance_url)
        self._tables = [_check_table(t) for t in tables]
        self._redactor = redactor
        self._entities = entities
        self._transport = transport or UnconfiguredTransport("Q-05", "servicenow")
        self._ctx = IngestContext(connector=CONNECTOR, run_id=run_id)

    @classmethod
    def from_settings(cls, settings: Settings, redactor: Redactor, *, transport: Transport | None = None,
                      entities: EntityResolver | None = None) -> "HttpServiceNowConnector":
        cfg = settings.connectors.get("servicenow") or {}
        tables = [str(t) for t in cfg.get("tables") or []] or list(RECORD_TYPES)
        return cls(instance_url=str(require(cfg.get("instance_url", ""), strict=settings.strict)), redactor=redactor,
                   tables=tables, entities=entities or EntityResolver.from_settings(settings), transport=transport)

    def _send(self, request: HttpRequest) -> Any:
        if is_placeholder(self._instance):
            not_implemented("Q-05", "servicenow instance url and integration user")
        return self._transport.send(request)

    def _rows(self, response: Any) -> list[dict[str, Any]]:
        result = response.get("result") if isinstance(response, Mapping) else None
        if isinstance(result, Mapping):
            return [dict(result)]
        return [dict(row) for row in result or []]

    def _normalize(self, raw: Mapping[str, Any], table: str) -> Signal:
        return normalize_servicenow_record(raw, redactor=self._redactor, instance_url=self._instance, table=table,
                                           entities=self._entities, ctx=self._ctx)

    def _poll_raw(self, cursor: str | None, limit: int) -> tuple[list[tuple[dict[str, Any], str]], str]:
        size = checked_limit(limit)
        watermarks = _decode_cursor(cursor)
        rows: list[tuple[dict[str, Any], str]] = []
        for table in self._tables:
            fetched = self._rows(self._send(self._api.table_delta(table, watermarks.get(table), limit=size)))
            rows += [(raw, table) for raw in fetched]
            newest = newest_timestamp(_value(raw.get("sys_updated_on")) for raw in fetched)
            if newest:
                watermarks[table] = newest
        return rows, _encode_cursor(watermarks)

    def poll_records(self, cursor: str | None, limit: int = 100) -> PollResult:
        """Per-table delta since the per-table ``sys_updated_on`` watermarks carried in the opaque cursor."""
        rows, next_cursor = self._poll_raw(cursor, limit)
        return PollResult(source=self.source, signals=[self._normalize(raw, table) for raw, table in rows],
                          next_cursor=next_cursor)

    def poll(self, cursor: str | None, limit: int = 100) -> PollResult:
        rows, next_cursor = self._poll_raw(cursor, limit)
        signals: list[Signal] = []
        for raw, table in rows:
            parent = self._normalize(raw, table)
            signals += [parent, *self._journal_for(parent, _value(raw.get("sys_id")))]
        return PollResult(source=self.source, signals=signals, next_cursor=next_cursor)

    def _fetch(self, record_id: str) -> tuple[dict[str, Any], str] | None:
        if _NUMBER.match(record_id):
            prefix = next(p for p in sorted(NUMBER_PREFIXES, key=len, reverse=True) if record_id.startswith(p))
            table = NUMBER_PREFIXES[prefix]
            rows = self._rows(self._send(self._api.by_number(table, record_id)))
            return (rows[0], table) if rows else None
        if _SYS_ID.match(record_id):
            for table in self._tables:
                try:
                    rows = self._rows(self._send(self._api.record(table, record_id)))
                except ConnectorError as exc:
                    if exc.code == "not_found":
                        continue
                    raise
                if rows:
                    return rows[0], table
        return None

    def get(self, record_id: str) -> Signal | None:
        fetched = self._fetch(record_id)
        return self._normalize(*fetched) if fetched else None

    def _journal_for(self, parent: Signal, sys_id: str) -> list[Signal]:
        rows = self._rows(self._send(self._api.journal(sys_id)))
        return [normalize_servicenow_journal(row, redactor=self._redactor, parent=parent, ctx=self._ctx)
                for row in rows]

    def journal(self, record_id: str) -> list[Signal]:
        fetched = self._fetch(record_id)
        if fetched is None:
            return []
        raw, table = fetched
        return self._journal_for(self._normalize(raw, table), _value(raw.get("sys_id")))
