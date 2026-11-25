"""Microsoft Graph connector (``mcp-msgraph`` reads, ``mcp-msgraph-write`` send shape) — docs/09 §5.

* Mail: ``messages/delta`` per designated mailbox folder → ``email_message`` signals (``source_version`` =
  ``changeKey``, permalink = ``webLink``; replies point at their thread root via ``parent_signal_id``; ``uniqueBody``
  is preferred so quoted history is not re-ingested).
* OneDrive/SharePoint: ``root/delta`` → ``document`` signals over server-side extracted text; Excel trackers → one
  ``tracker_row`` signal per row with a cell-range permalink (content-addressed row versions).
* ``GraphApi`` builds delta / content / workbook / ``sendMail`` requests; continuation cursors are Graph
  ``@odata.nextLink``/``deltaLink`` URLs and are host-checked (no SSRF through a crafted cursor).
* Mailboxes, sites and the app registration are Q-13 (tenant Q-08); Workbook API app-only support is UNCONFIRMED.

Open item: ``PLACEHOLDER:Q-13:msgraph_app_credentials`` (app registration, RBAC for Applications scope).
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any
from urllib.parse import quote, unquote, urlsplit

from pm_agent.connectors.base import ConnectorError, PollResult, make_signal
from pm_agent.connectors.common import (
    as_mapping,
    FixtureConnectorBase,
    FixtureUnit,
    IngestContext,
    context_or,
    default_fixture_root,
    first_text,
    checked_limit,
    parse_utc,
    person_ref,
    text_of,
)
from pm_agent.connectors.entities import EntityResolver, resolve_customer, resolve_product
from pm_agent.connectors.http import HttpRequest, Transport, UnconfiguredTransport
from pm_agent.grounding.redaction import Redactor
from pm_agent.placeholders import is_placeholder, not_implemented, require
from pm_agent.schemas import Customer, RecordType, Sensitivity, Signal, SourceSystem, sha256_hex
from pm_agent.settings import Settings

GRAPH_BASE = "https://graph.microsoft.com/v1.0"
GRAPH_HOST = "graph.microsoft.com"
QUESTION = "Q-13"
CONNECTOR = "mcp-msgraph"
MAIL_SELECT = ("id,changeKey,subject,body,uniqueBody,conversationId,internetMessageId,from,toRecipients,"
               "receivedDateTime,webLink")
ACTION_HEADER = "X-PM-Agent-Action-Id"
_CELL_RANGE = re.compile(r"^\$?([A-Z]{1,3})\$?(\d+)(?::\$?([A-Z]{1,3})\$?(\d+))?$")
_TITLE_COLUMNS = ("issue", "title", "summary", "subject")


def outlook_message_permalink(message_id: str) -> str:
    """Outlook on the web deep link (fallback when ``webLink`` is absent) [K]."""
    return f"https://outlook.office365.com/owa/?ItemID={quote(message_id, safe='')}&exvsurl=1&viewmodel=ReadMessageItem"


def excel_range_permalink(web_url: str, sheet: str, address: str) -> str:
    """Workbook link that opens on a cell range (``activeCell`` URL parameter — [K], UNCONFIRMED for SharePoint)."""
    separator = "&" if "?" in web_url else "?"
    reference = "'" + sheet.replace("'", "''") + "'!" + address
    return f"{web_url}{separator}web=1&activeCell={quote(reference, safe='')}"


def _address(item: Mapping[str, Any]) -> str | None:
    email = as_mapping(item.get("emailAddress"))
    return first_text(email.get("address")) or None


def _sensitivity(item: Mapping[str, Any]) -> Sensitivity:
    label = item.get("sensitivityLabel")
    name = first_text(label.get("displayName"), label.get("labelId")) if isinstance(label, Mapping) else ""
    return Sensitivity(purview_label=name or None)


def normalize_graph_message(raw: Mapping[str, Any], *, redactor: Redactor, thread_root: Signal | None = None,
                            entities: EntityResolver | None = None, ctx: IngestContext | None = None) -> Signal:
    """One Outlook message → ``email_message`` signal; replies link to *thread_root* (``conversationId`` root)."""
    message_id = first_text(raw.get("id"))
    if not message_id:
        raise ConnectorError("validation_failed", "message without id")
    body = raw.get("uniqueBody") or raw.get("body") or {}
    text = text_of(first_text(body.get("content")), first_text(body.get("contentType"))) \
        if isinstance(body, Mapping) else ""
    subject = first_text(raw.get("subject")) or None
    is_reply = thread_root is not None and thread_root.source_record_id != message_id
    customer = resolve_customer(entities, None, subject, text) or (thread_root.customer if is_reply and thread_root
                                                                  else None)
    product = resolve_product(entities, subject, text) or (thread_root.product if is_reply and thread_root else None)
    sender = as_mapping(raw.get("from"))
    return make_signal(
        redactor=redactor, source_system=SourceSystem.EMAIL, source_record_id=message_id,
        source_version=first_text(raw.get("changeKey"), raw.get("@odata.etag")) or "0",
        record_type=RecordType.EMAIL_MESSAGE,
        permalink=first_text(raw.get("webLink")) or outlook_message_permalink(message_id),
        raw_text=text or (subject or ""), title=subject,
        captured_utc=parse_utc(raw.get("receivedDateTime")) or parse_utc(raw.get("createdDateTime")),
        author=person_ref(redactor, _address(sender)), customer=customer, product=product,
        parent_signal_id=thread_root.signal_id if is_reply and thread_root else None,
        **context_or(ctx, CONNECTOR).stamp())


def normalize_graph_document(raw_item: Mapping[str, Any], *, redactor: Redactor, text: str,
                             entities: EntityResolver | None = None, ctx: IngestContext | None = None) -> Signal:
    """One drive item + its server-side extracted text → ``document`` signal (``source_version`` = eTag)."""
    item_id = first_text(raw_item.get("id"))
    web_url = first_text(raw_item.get("webUrl"))
    if not item_id or not web_url:
        raise ConnectorError("validation_failed", "drive item without id/webUrl")
    title = first_text(raw_item.get("name")) or None
    modified_by = (raw_item.get("lastModifiedBy") or {}).get("user") or {}
    return make_signal(
        redactor=redactor, source_system=SourceSystem.ONEDRIVE, source_record_id=item_id,
        source_version=first_text(raw_item.get("eTag"), raw_item.get("cTag")).strip('"') or "0",
        record_type=RecordType.DOCUMENT, permalink=web_url, raw_text=text_of(text, "text") or (title or ""),
        title=title, captured_utc=parse_utc(raw_item.get("lastModifiedDateTime")),
        author=person_ref(redactor, first_text(modified_by.get("email"), modified_by.get("id")) or None),
        customer=resolve_customer(entities, None, title, text), product=resolve_product(entities, title, text),
        sensitivity=_sensitivity(raw_item), **context_or(ctx, CONNECTOR).stamp())


def _range_origin(address: str) -> tuple[str, int, str]:
    cells = address.rsplit("!", 1)[-1]
    match = _CELL_RANGE.match(cells)
    if match is None:
        return "A", 1, "A"
    start_col, start_row, end_col = match.group(1), int(match.group(2)), match.group(3) or match.group(1)
    return start_col, start_row, end_col


def _row_entities(entities: EntityResolver | None, header: Sequence[str], cells: Sequence[str],
                  text: str) -> tuple[Customer | None, str | None]:
    by_name = {h.strip().lower(): c for h, c in zip(header, cells)}
    customer_cell = by_name.get("customer") or by_name.get("customers") or by_name.get("account")
    product_cell = by_name.get("product")
    if entities is None:
        return None, None
    customer = entities.customer(customer_cell) if customer_cell is not None else entities.customer(text)
    product = entities.product(product_cell) if product_cell is not None else entities.product(text)
    return customer, product


def normalize_workbook_rows(raw_item: Mapping[str, Any], range_json: Mapping[str, Any], *, redactor: Redactor,
                            sheet: str, entities: EntityResolver | None = None, ctx: IngestContext | None = None,
                            header_row: bool = True) -> list[Signal]:
    """A worksheet range (``usedRange`` values) → one ``tracker_row`` signal per non-empty row."""
    item_id = first_text(raw_item.get("id"))
    web_url = first_text(raw_item.get("webUrl"))
    values = [[("" if cell is None else str(cell)) for cell in row] for row in range_json.get("values") or []]
    if not item_id or not web_url or not values:
        return []
    start_col, start_row, end_col = _range_origin(first_text(range_json.get("address")))
    header = values[0] if header_row else [f"Column {i + 1}" for i in range(len(values[0]))]
    rows = values[1:] if header_row else values
    first_data_row = start_row + (1 if header_row else 0)
    stamp = context_or(ctx, CONNECTOR).stamp()
    signals = []
    for offset, cells in enumerate(rows):
        if not any(cell.strip() for cell in cells):
            continue
        row_number = first_data_row + offset
        text = "\n".join(f"{name}: {cell}" for name, cell in zip(header, cells) if cell.strip())
        customer, product = _row_entities(entities, header, cells, text)
        title = next((cell for name, cell in zip(header, cells) if name.strip().lower() in _TITLE_COLUMNS
                      and cell.strip()), None)
        signals.append(make_signal(
            redactor=redactor, source_system=SourceSystem.ONEDRIVE, source_record_id=f"{item_id}!{sheet}!{row_number}",
            source_version=sha256_hex(json.dumps(cells, ensure_ascii=False))[:16], record_type=RecordType.TRACKER_ROW,
            permalink=excel_range_permalink(web_url, sheet, f"{start_col}{row_number}:{end_col}{row_number}"),
            raw_text=text, title=title, captured_utc=parse_utc(raw_item.get("lastModifiedDateTime")),
            customer=customer, product=product, sensitivity=_sensitivity(raw_item), **stamp))
    return signals


class GraphApi:
    """Pure request builders for Microsoft Graph v1.0 [K]."""

    def __init__(self, base_url: str = GRAPH_BASE) -> None:
        self._base = base_url.rstrip("/")

    @staticmethod
    def continuation(cursor: str, *, expected_path: str | None = None) -> str:
        """Accept only Graph-issued ``nextLink``/``deltaLink`` URLs bound to this resource (SSRF + confused deputy)."""
        parts = urlsplit(cursor)
        if parts.scheme != "https" or parts.hostname != GRAPH_HOST or parts.username or parts.port or parts.fragment:
            raise ConnectorError("validation_failed", "cursor is not a Microsoft Graph continuation link")
        for segment in parts.path.split("/"):
            decoded = unquote(segment)
            if segment in (".", "..") or decoded in (".", "..") or "/" in decoded or "\\" in decoded:
                raise ConnectorError("validation_failed", "cursor path contains a traversal segment")
        if expected_path is not None:
            actual, expected = unquote(parts.path), unquote(expected_path)
            if actual != expected and not actual.startswith(f"{expected}("):
                raise ConnectorError("validation_failed", "cursor does not belong to the configured Graph resource")
        return cursor

    def _user(self, mailbox: str) -> str:
        return f"{self._base}/users/{quote(mailbox, safe='@')}"

    def _drive_item(self, drive_id: str, item_id: str) -> str:
        return f"{self._base}/drives/{quote(drive_id, safe='!')}/items/{quote(item_id, safe='')}"

    def mail_delta(self, mailbox: str, folder: str, *, page_size: int, cursor: str | None = None) -> HttpRequest:
        headers = {"Prefer": f'odata.maxpagesize={page_size}, outlook.body-content-type="html"'}
        delta_url = f"{self._user(mailbox)}/mailFolders/{quote(folder, safe='')}/messages/delta"
        if cursor:
            return HttpRequest(method="GET", url=self.continuation(cursor, expected_path=urlsplit(delta_url).path),
                               headers=headers)
        return HttpRequest(method="GET", url=delta_url, params=(("$select", MAIL_SELECT),), headers=headers)

    def message(self, mailbox: str, message_id: str) -> HttpRequest:
        return HttpRequest(method="GET", url=f"{self._user(mailbox)}/messages/{quote(message_id, safe='')}",
                           params=(("$select", MAIL_SELECT),))

    def drive_delta(self, drive_id: str, *, page_size: int, cursor: str | None = None) -> HttpRequest:
        headers = {"Prefer": f"odata.maxpagesize={page_size}"}
        delta_url = f"{self._base}/drives/{quote(drive_id, safe='!')}/root/delta"
        if cursor:
            return HttpRequest(method="GET", url=self.continuation(cursor, expected_path=urlsplit(delta_url).path),
                               headers=headers)
        return HttpRequest(method="GET", url=delta_url, headers=headers)

    def item_content(self, drive_id: str, item_id: str) -> HttpRequest:
        return HttpRequest(method="GET", url=f"{self._drive_item(drive_id, item_id)}/content")

    def worksheet_used_range(self, drive_id: str, item_id: str, sheet: str) -> HttpRequest:
        return HttpRequest(method="GET", url=(f"{self._drive_item(drive_id, item_id)}/workbook/worksheets/"
                                              f"{quote(sheet, safe='')}/usedRange(valuesOnly=true)"))

    def send_mail(self, mailbox: str, *, to: Sequence[str], cc: Sequence[str], subject: str, body: str,
                  action_id: str, content_type: str = "Text") -> HttpRequest:
        """``POST /users/{shared-mailbox}/sendMail``; the action id rides in a custom header (idempotency marker)."""
        message = {
            "subject": subject,
            "body": {"contentType": content_type, "content": body},
            "toRecipients": [{"emailAddress": {"address": address}} for address in to],
            "ccRecipients": [{"emailAddress": {"address": address}} for address in cc],
            "internetMessageHeaders": [{"name": ACTION_HEADER, "value": action_id}],
        }
        return HttpRequest(method="POST", url=f"{self._user(mailbox)}/sendMail",
                           headers={"Content-Type": "application/json"},
                           json_body={"message": message, "saveToSentItems": True})


def _thread_signals(messages: Sequence[Mapping[str, Any]], *, redactor: Redactor, entities: EntityResolver | None,
                    ctx: IngestContext) -> list[Signal]:
    """Normalize messages in delivery order; each reply links to the earliest message of its conversation."""
    roots: dict[str, Mapping[str, Any]] = {}
    for raw in sorted(messages, key=lambda m: first_text(m.get("receivedDateTime"))):
        roots.setdefault(first_text(raw.get("conversationId"), raw.get("id")), raw)
    root_signals = {conv: normalize_graph_message(raw, redactor=redactor, entities=entities, ctx=ctx)
                    for conv, raw in roots.items()}
    signals = []
    for raw in messages:
        root = root_signals[first_text(raw.get("conversationId"), raw.get("id"))]
        signals.append(root if root.source_record_id == first_text(raw.get("id")) else
                       normalize_graph_message(raw, redactor=redactor, thread_root=root, entities=entities, ctx=ctx))
    return signals


class FixtureGraphMailConnector(FixtureConnectorBase):
    """Designated-mailbox mail over a synthetic ``messages/delta`` page (``source`` = ``email``)."""

    def __init__(self, redactor: Redactor, *, path: Path | None = None, entities: EntityResolver | None = None,
                 run_id: str = "fixture") -> None:
        data = json.loads((path or default_fixture_root() / "msgraph" / "mail.json").read_text(encoding="utf-8"))
        ctx = IngestContext(connector="fixture-msgraph-mail", run_id=run_id)
        signals = _thread_signals(list(data["delta"]["value"]), redactor=redactor, entities=entities, ctx=ctx)
        super().__init__(SourceSystem.EMAIL, [FixtureUnit(parent=s) for s in signals])


class FixtureGraphDriveConnector(FixtureConnectorBase):
    """OneDrive/SharePoint documents and Excel tracker rows over synthetic JSON (``source`` = ``onedrive``)."""

    def __init__(self, redactor: Redactor, *, path: Path | None = None, entities: EntityResolver | None = None,
                 run_id: str = "fixture") -> None:
        data = json.loads((path or default_fixture_root() / "msgraph" / "drive.json").read_text(encoding="utf-8"))
        ctx = IngestContext(connector="fixture-msgraph-drive", run_id=run_id)
        self._documents: dict[str, Signal] = {}
        self._rows: dict[str, list[Signal]] = {}
        units = []
        for item in data["delta"]["value"]:
            item_id = first_text(item.get("id"))
            sheets = (data.get("workbooks") or {}).get(item_id)
            if sheets:
                rows = [row for sheet, rng in sheets.items()
                        for row in normalize_workbook_rows(item, rng, redactor=redactor, sheet=sheet,
                                                           entities=entities, ctx=ctx)]
                self._rows[item_id] = rows
                units += [FixtureUnit(parent=row) for row in rows]
            elif item_id in (data.get("item_text") or {}):
                document = normalize_graph_document(item, redactor=redactor, text=data["item_text"][item_id],
                                                    entities=entities, ctx=ctx)
                self._documents[item_id] = document
                units.append(FixtureUnit(parent=document))
        super().__init__(SourceSystem.ONEDRIVE, units)

    def item_text(self, item_id: str) -> Signal | None:
        return self._documents.get(item_id)

    def workbook_rows(self, item_id: str, sheet: str | None = None) -> list[Signal]:
        rows = self._rows.get(item_id, [])
        return [r for r in rows if sheet is None or r.source_record_id.split("!")[1] == sheet]


class _GraphBase:
    def __init__(self, *, redactor: Redactor, entities: EntityResolver | None, transport: Transport | None,
                 run_id: str) -> None:
        self._api = GraphApi()
        self._redactor = redactor
        self._entities = entities
        self._transport = transport or UnconfiguredTransport("Q-13", "msgraph")
        self._ctx = IngestContext(connector=CONNECTOR, run_id=run_id)

    @staticmethod
    def _scope(values: object, settings: Settings) -> list[str]:
        resolved = require(values, strict=settings.strict)
        if isinstance(resolved, list):
            return [str(v) for v in resolved]
        return [str(resolved)] if resolved else []

    @staticmethod
    def _configured(scope: Sequence[str], what: str) -> None:
        if not scope or any(is_placeholder(s) for s in scope):
            not_implemented("Q-13", what)


class HttpGraphMailConnector(_GraphBase):
    """Mail delta for designated shared mailboxes (``Mail.Read`` scoped by Exchange RBAC for Applications)."""

    source = SourceSystem.EMAIL

    def __init__(self, *, mailboxes: Sequence[str], redactor: Redactor, folder: str = "inbox",
                 entities: EntityResolver | None = None, transport: Transport | None = None,
                 run_id: str = "adhoc") -> None:
        super().__init__(redactor=redactor, entities=entities, transport=transport, run_id=run_id)
        self._mailboxes = list(mailboxes)
        self._folder = folder

    @classmethod
    def from_settings(cls, settings: Settings, redactor: Redactor, *, transport: Transport | None = None,
                      entities: EntityResolver | None = None) -> "HttpGraphMailConnector":
        cfg = settings.connectors.get("msgraph") or {}
        return cls(mailboxes=cls._scope(cfg.get("mailboxes"), settings), redactor=redactor,
                   entities=entities or EntityResolver.from_settings(settings), transport=transport)

    def poll(self, cursor: str | None, limit: int = 100) -> PollResult:
        """One delta page of the first mailbox (cursor = Graph nextLink/deltaLink)."""
        self._configured(self._mailboxes, "mailboxes in scope for mail delta")
        response = self._transport.send(self._api.mail_delta(self._mailboxes[0], self._folder,
                                                             page_size=checked_limit(limit), cursor=cursor))
        messages = [m for m in response.get("value") or [] if "@removed" not in m]
        signals = _thread_signals(messages, redactor=self._redactor, entities=self._entities, ctx=self._ctx)
        next_cursor = first_text(response.get("@odata.nextLink"), response.get("@odata.deltaLink")) or cursor
        return PollResult(source=self.source, signals=signals, next_cursor=next_cursor)

    poll_records = poll

    def get(self, record_id: str) -> Signal | None:
        """Re-read one message by Graph id (webhook hint) from the first designated mailbox."""
        self._configured(self._mailboxes, "mailboxes in scope for message reads")
        try:
            raw = self._transport.send(self._api.message(self._mailboxes[0], record_id))
        except ConnectorError as exc:
            if exc.code == "not_found":
                return None
            raise
        return normalize_graph_message(raw, redactor=self._redactor, entities=self._entities, ctx=self._ctx)


class HttpGraphDriveConnector(_GraphBase):
    """Drive delta for ``Sites.Selected`` sites; document text and workbook rows (Workbook API app-only UNCONFIRMED)."""

    source = SourceSystem.ONEDRIVE

    def __init__(self, *, drives: Sequence[str], redactor: Redactor, entities: EntityResolver | None = None,
                 transport: Transport | None = None, run_id: str = "adhoc") -> None:
        super().__init__(redactor=redactor, entities=entities, transport=transport, run_id=run_id)
        self._drives = list(drives)

    @classmethod
    def from_settings(cls, settings: Settings, redactor: Redactor, *, transport: Transport | None = None,
                      entities: EntityResolver | None = None) -> "HttpGraphDriveConnector":
        cfg = settings.connectors.get("msgraph") or {}
        return cls(drives=cls._scope(cfg.get("sites"), settings), redactor=redactor,
                   entities=entities or EntityResolver.from_settings(settings), transport=transport)

    def poll(self, cursor: str | None, limit: int = 100) -> PollResult:
        """One drive delta page; spreadsheets become tracker rows, other files documents (text extraction Q-13)."""
        self._configured(self._drives, "SharePoint/OneDrive sites (drives) in scope")
        drive = self._drives[0]
        response = self._transport.send(self._api.drive_delta(drive, page_size=checked_limit(limit), cursor=cursor))
        signals: list[Signal] = []
        for item in response.get("value") or []:
            if "file" not in item or "deleted" in item:
                continue
            signals += self._item_signals(drive, item)
        next_cursor = first_text(response.get("@odata.nextLink"), response.get("@odata.deltaLink")) or cursor
        return PollResult(source=self.source, signals=signals, next_cursor=next_cursor)

    poll_records = poll

    def _item_signals(self, drive: str, item: Mapping[str, Any]) -> list[Signal]:
        item_id = first_text(item.get("id"))
        if first_text(item.get("name")).lower().endswith(".xlsx"):
            return self.workbook_rows(item_id, item=item, drive=drive)
        not_implemented("Q-13", "server-side document text extraction (docx/pptx/pdf) for drive items")

    def workbook_rows(self, item_id: str, sheet: str | None = None, *, item: Mapping[str, Any] | None = None,
                      drive: str | None = None) -> list[Signal]:
        """Rows of one worksheet (default ``Tracker``) through the Workbook API ``usedRange`` read."""
        self._configured(self._drives, "SharePoint/OneDrive sites (drives) in scope")
        target = drive or self._drives[0]
        name = sheet or "Tracker"
        used = self._transport.send(self._api.worksheet_used_range(target, item_id, name))
        return normalize_workbook_rows(item or {"id": item_id}, used, redactor=self._redactor, sheet=name,
                                       entities=self._entities, ctx=self._ctx)

    def item_text(self, item_id: str) -> Signal | None:
        del item_id
        self._configured(self._drives, "SharePoint/OneDrive sites (drives) in scope")
        not_implemented("Q-13", "server-side document text extraction (docx/pptx/pdf) for drive items")

    def get(self, record_id: str) -> Signal | None:
        del record_id
        self._configured(self._drives, "SharePoint/OneDrive sites (drives) in scope")
        not_implemented("Q-13", "drive item re-read (GET /drives/{d}/items/{i}) and text extraction")
