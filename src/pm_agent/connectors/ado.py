"""Azure DevOps connector (``mcp-ado`` reads, ``mcp-ado-write`` request shapes) — docs/09 §2.

* Normalizers: work item → ``work_item`` signal (``source_version`` = rev, permalink ``…/_workitems/edit/{id}``);
  comment → child ``comment`` signal (``parent_signal_id`` = the work item version it was read with).
* ``AdoApi``: request builders for the verified REST 7.1 shapes — WIQL delta (ids only, ``timePrecision``),
  ``workitemsbatch`` (≤ 200 ids, ``$expand=Relations``), comments (7.1-preview.4), create as JSON Patch with
  ``validateOnly`` pre-flight (``bypassRules`` is never sent) and relation adds guarded by ``test /rev``.
* ``FixtureAdoConnector`` (synthetic JSON) and ``HttpAdoConnector`` (real client; transport/credentials are Q-04).
  ``workitemsbatch`` is sent with ``errorPolicy=Omit``, so a deleted or inaccessible id comes back as ``null``: such
  entries are dropped and ``get`` reports the item as not found. Area paths narrow the change delta; a placeholder
  scope is *unconfigured* (every call refuses with Q-04), never "the whole project".

Open item: ``PLACEHOLDER:Q-04:ado_rest_credentials`` (Entra service principals for the read/write servers).
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
    html_to_text,
    newest_timestamp,
    checked_limit,
    parse_utc,
    person_ref,
    require_timestamp,
)
from pm_agent.connectors.entities import EntityResolver, resolve_customer, resolve_product
from pm_agent.connectors.http import HttpRequest, Transport, UnconfiguredTransport
from pm_agent.grounding.redaction import Redactor
from pm_agent.placeholders import is_placeholder, not_implemented, require
from pm_agent.schemas import RecordType, Signal, SignalTypeHint, SourceSystem
from pm_agent.settings import Settings

API_VERSION = "7.1"
COMMENTS_API_VERSION = "7.1-preview.4"
MAX_BATCH = 200
MAX_WORK_ITEM_ID = 2**31 - 1  # work item ids are positive int32 (REST 7.1)
QUESTION = "Q-04"
CONNECTOR = "mcp-ado"
RELATION_TYPES = frozenset({"System.LinkTypes.Related", "Hyperlink"})
_FIELD_NAME = re.compile(r"[A-Za-z][A-Za-z0-9_.]*")
_WORK_ITEM_ID = re.compile(r"[1-9][0-9]{0,9}")
_TEXT_FIELDS = (("System.Description", "Description"), ("Microsoft.VSTS.TCM.ReproSteps", "Repro steps"),
                ("Microsoft.VSTS.Common.AcceptanceCriteria", "Acceptance criteria"),
                ("Microsoft.VSTS.TCM.SystemInfo", "System info"))
_TYPE_HINTS = {"bug": SignalTypeHint.DEFECT, "feature": SignalTypeHint.FEATURE_REQUEST,
               "user story": SignalTypeHint.FEATURE_REQUEST, "product backlog item": SignalTypeHint.FEATURE_REQUEST}
_EMAIL_IN_ANGLES = re.compile(r"<([^<>@\s]+@[^<>\s]+)>")


def ado_permalink(organization: str, project: str, work_item_id: int | str) -> str:
    """``https://dev.azure.com/{org}/{project}/_workitems/edit/{id}`` (docs/09 §2)."""
    return (f"https://dev.azure.com/{quote(organization, safe='')}/{quote(project, safe='')}/_workitems/edit/"
            f"{quote(str(work_item_id), safe='')}")


def parse_work_item_id(value: str) -> int | None:
    """*value* as a work item id — ASCII digits, no sign/space/leading zero, positive int32 — else ``None``.

    ``str.isdigit`` is not enough: ``"²".isdigit()`` is true but ``int("²")`` raises, and ``"٣"`` would silently read
    work item 3.
    """
    if not _WORK_ITEM_ID.fullmatch(value):
        return None
    number = int(value)
    return number if number <= MAX_WORK_ITEM_ID else None


def _identity(value: object) -> str | None:
    """ADO identity (object with ``uniqueName`` or legacy ``"Name <email>"`` string) → stable identifier."""
    if isinstance(value, Mapping):
        return first_text(value.get("uniqueName"), value.get("id")) or None
    if isinstance(value, str):
        match = _EMAIL_IN_ANGLES.search(value)
        return match.group(1) if match else (value or None)
    return None


def _work_item_text(fields: Mapping[str, Any]) -> str:
    sections = []
    for name, heading in _TEXT_FIELDS:
        value = fields.get(name)
        text = html_to_text(value) if isinstance(value, str) else ""
        if text:
            sections.append(f"{heading}:\n{text}")
    return "\n\n".join(sections)


def normalize_ado_work_item(raw: Mapping[str, Any], *, redactor: Redactor, organization: str, project: str,
                            entities: EntityResolver | None = None, ctx: IngestContext | None = None) -> Signal:
    """One work item revision (``workitemsbatch`` element) → redacted ``work_item`` signal."""
    fields = as_mapping(raw.get("fields"))
    work_item_id = first_text(raw.get("id"), fields.get("System.Id"))
    if not work_item_id:
        raise ConnectorError("validation_failed", "work item without id")
    title = first_text(fields.get("System.Title")) or None
    text = _work_item_text(fields)
    area = first_text(fields.get("System.AreaPath"))
    tags = first_text(fields.get("System.Tags"))
    work_item_type = first_text(fields.get("System.WorkItemType")).strip().lower()
    return make_signal(
        redactor=redactor, source_system=SourceSystem.ADO, source_record_id=work_item_id,
        source_version=first_text(raw.get("rev"), fields.get("System.Rev")) or "0",
        record_type=RecordType.WORK_ITEM, permalink=ado_permalink(organization, project, work_item_id),
        raw_text=text or (title or ""), title=title,
        captured_utc=parse_utc(fields.get("System.ChangedDate")) or parse_utc(fields.get("System.CreatedDate")),
        author=person_ref(redactor, _identity(fields.get("System.CreatedBy"))),
        customer=resolve_customer(entities, None, title, text), product=resolve_product(entities, area, title, text,
                                                                                         tags),
        type_hint=_TYPE_HINTS.get(work_item_type, SignalTypeHint.UNKNOWN), **context_or(ctx, CONNECTOR).stamp())


def normalize_ado_comment(raw: Mapping[str, Any], *, redactor: Redactor, parent: Signal,
                          ctx: IngestContext | None = None) -> Signal:
    """One work item comment → child ``comment`` signal of *parent* (the work item version it was read with)."""
    comment_id = first_text(raw.get("id"))
    if not comment_id:
        raise ConnectorError("validation_failed", "comment without id")
    work_item_id = first_text(raw.get("workItemId")) or parent.source_record_id
    return make_signal(
        redactor=redactor, source_system=SourceSystem.ADO, source_record_id=f"{work_item_id}:{comment_id}",
        source_version=first_text(raw.get("version")) or "1", record_type=RecordType.COMMENT,
        permalink=f"{parent.permalink}#comment-{quote(comment_id, safe='')}",
        raw_text=html_to_text(first_text(raw.get("text"))),
        captured_utc=parse_utc(raw.get("modifiedDate")) or parse_utc(raw.get("createdDate")),
        author=person_ref(redactor, _identity(raw.get("createdBy"))), customer=parent.customer, product=parent.product,
        parent_signal_id=parent.signal_id, **context_or(ctx, CONNECTOR).stamp())


def _wiql_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


class AdoApi:
    """Pure request builders for Azure DevOps REST 7.1 (no I/O)."""

    def __init__(self, organization: str, project: str, api_version: str = API_VERSION,
                 base_url: str = "https://dev.azure.com") -> None:
        self.organization = organization
        self.project = project
        self.api_version = api_version
        self._root = f"{base_url.rstrip('/')}/{quote(organization, safe='')}/{quote(project, safe='')}"

    @staticmethod
    def wiql_changed_text(since: str | None, *, area_paths: Sequence[str] = ()) -> str:
        """WIQL for the change delta (``System.ChangedDate >= cursor``); literals are escaped, never interpolated."""
        clauses = ["[System.TeamProject] = @project"]
        if since:
            clauses.append(f"[System.ChangedDate] >= {_wiql_literal(since)}")
        if area_paths:
            clauses.append("(" + " OR ".join(f"[System.AreaPath] UNDER {_wiql_literal(p)}" for p in area_paths) + ")")
        return ("SELECT [System.Id], [System.Rev], [System.ChangedDate] FROM WorkItems WHERE "
                + " AND ".join(clauses) + " ORDER BY [System.ChangedDate] ASC")

    def wiql_changed_since(self, since: str | None, *, top: int, area_paths: Sequence[str] = ()) -> HttpRequest:
        return HttpRequest(method="POST", url=f"{self._root}/_apis/wit/wiql",
                           params=(("api-version", self.api_version), ("timePrecision", "true"), ("$top", str(top))),
                           headers={"Content-Type": "application/json"},
                           json_body={"query": self.wiql_changed_text(since, area_paths=area_paths)})

    def wiql_search(self, text: str, *, top: int) -> HttpRequest:
        """Prior-art lookup by words in title/description ([K]; the ADO Search API is an alternative)."""
        words = _wiql_literal(" ".join(text.split())[:256])
        query = ("SELECT [System.Id] FROM WorkItems WHERE [System.TeamProject] = @project AND "
                 f"([System.Title] CONTAINS WORDS {words} OR [System.Description] CONTAINS WORDS {words}) "
                 "ORDER BY [System.ChangedDate] DESC")
        return HttpRequest(method="POST", url=f"{self._root}/_apis/wit/wiql",
                           params=(("api-version", self.api_version), ("$top", str(top))),
                           headers={"Content-Type": "application/json"}, json_body={"query": query})

    def workitems_batch(self, ids: Sequence[int]) -> HttpRequest:
        if not ids or len(ids) > MAX_BATCH:
            raise ValueError(f"workitemsbatch takes 1..{MAX_BATCH} ids, got {len(ids)}")
        return HttpRequest(method="POST", url=f"{self._root}/_apis/wit/workitemsbatch",
                           params=(("api-version", self.api_version),), headers={"Content-Type": "application/json"},
                           json_body={"ids": [int(i) for i in ids], "$expand": "Relations", "errorPolicy": "Omit"})

    def comments(self, work_item_id: int | str, *, top: int = 200,
                 continuation_token: str | None = None) -> HttpRequest:
        params = [("api-version", COMMENTS_API_VERSION), ("$top", str(top))]
        if continuation_token:
            params.append(("continuationToken", continuation_token))
        return HttpRequest(method="GET", url=f"{self._root}/_apis/wit/workItems/{int(work_item_id)}/comments",
                           params=tuple(params))

    def _patch_params(self, validate_only: bool) -> tuple[tuple[str, str], ...]:
        return (("api-version", self.api_version),) + ((("validateOnly", "true"),) if validate_only else ())

    @staticmethod
    def _relation_ops(relations: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
        ops = []
        for relation in relations:
            rel = str(relation.get("rel", ""))
            if rel not in RELATION_TYPES:
                raise ValueError(f"relation type {rel!r} is not allowed (only {sorted(RELATION_TYPES)})")
            value: dict[str, Any] = {"rel": rel, "url": str(relation["url"])}
            if relation.get("attributes"):
                value["attributes"] = dict(relation["attributes"])
            ops.append({"op": "add", "path": "/relations/-", "value": value})
        return ops

    def create_work_item(self, work_item_type: str, fields: Mapping[str, Any], *, validate_only: bool,
                         relations: Sequence[Mapping[str, Any]] = (), idempotency_tag: str | None = None
                         ) -> HttpRequest:
        """``POST …/workitems/${type}`` JSON Patch; ``validateOnly=true`` is the pre-flight (docs/07 §2)."""
        values = dict(fields)
        if idempotency_tag:
            existing = str(values.get("System.Tags") or "").strip()
            values["System.Tags"] = f"{existing}; {idempotency_tag}" if existing else idempotency_tag
        ops: list[dict[str, Any]] = []
        for name, value in values.items():
            if not _FIELD_NAME.fullmatch(name):
                raise ValueError(f"invalid field reference name {name!r}")
            ops.append({"op": "add", "path": f"/fields/{name}", "value": value})
        ops += self._relation_ops(relations)
        return HttpRequest(method="POST",
                           url=f"{self._root}/_apis/wit/workitems/${quote(work_item_type, safe='')}",
                           params=self._patch_params(validate_only),
                           headers={"Content-Type": "application/json-patch+json"}, json_body=ops)

    def add_relations(self, work_item_id: int | str, *, rev: int, relations: Sequence[Mapping[str, Any]],
                      validate_only: bool) -> HttpRequest:
        """``PATCH …/workitems/{id}``: ``test /rev`` guard, then ``/relations/-`` adds (Related / Hyperlink only)."""
        ops = [{"op": "test", "path": "/rev", "value": int(rev)}, *self._relation_ops(relations)]
        return HttpRequest(method="PATCH", url=f"{self._root}/_apis/wit/workitems/{int(work_item_id)}",
                           params=self._patch_params(validate_only),
                           headers={"Content-Type": "application/json-patch+json"}, json_body=ops)


class FixtureAdoConnector(FixtureConnectorBase):
    """ADO over synthetic ``workitemsbatch`` + comments JSON (``tests/fixtures/connectors/ado``)."""

    def __init__(self, redactor: Redactor, *, path: Path | None = None, entities: EntityResolver | None = None,
                 run_id: str = "fixture") -> None:
        data = json.loads((path or default_fixture_root() / "ado" / "workitems.json").read_text(encoding="utf-8"))
        meta = data.get("_meta") or {}
        ctx = IngestContext(connector="fixture-ado", run_id=run_id)
        units = []
        for raw in data["workitemsbatch"]["value"]:
            parent = normalize_ado_work_item(raw, redactor=redactor, organization=meta["organization"],
                                             project=meta["project"], entities=entities, ctx=ctx)
            comments = (data.get("comments") or {}).get(parent.source_record_id) or {}
            children = tuple(normalize_ado_comment(c, redactor=redactor, parent=parent, ctx=ctx)
                             for c in comments.get("comments") or [])
            units.append(FixtureUnit(parent=parent, children=children))
        super().__init__(SourceSystem.ADO, units)

    def comments(self, work_item_id: str) -> list[Signal]:
        return self.children(str(work_item_id))


class HttpAdoConnector:
    """Azure DevOps REST client (Entra service principal — credentials and sandbox are Q-04)."""

    source = SourceSystem.ADO

    def __init__(self, *, organization: str, project: str, redactor: Redactor, api_version: str = API_VERSION,
                 area_paths: Sequence[str] = (), entities: EntityResolver | None = None,
                 transport: Transport | None = None, run_id: str = "adhoc") -> None:
        self._organization = organization
        self._project = project
        self._api = AdoApi(organization, project, api_version)
        self._area_paths = list(area_paths)
        self._redactor = redactor
        self._entities = entities
        self._transport = transport or UnconfiguredTransport("Q-04", "ado")
        self._ctx = IngestContext(connector=CONNECTOR, run_id=run_id)

    @classmethod
    def from_settings(cls, settings: Settings, redactor: Redactor, *, transport: Transport | None = None,
                      entities: EntityResolver | None = None) -> "HttpAdoConnector":
        cfg = settings.connectors.get("ado") or {}
        areas = require(cfg.get("area_paths"), strict=settings.strict)
        # A placeholder stays in the scope list so every call refuses (``_send``) instead of reading the whole project.
        area_paths = [str(a) for a in areas] if isinstance(areas, list) else ([] if areas is None else [str(areas)])
        return cls(organization=str(require(cfg.get("organization", ""), strict=settings.strict)),
                   project=str(require(cfg.get("project", ""), strict=settings.strict)), redactor=redactor,
                   api_version=str(cfg.get("api_version") or API_VERSION), area_paths=area_paths,
                   entities=entities or EntityResolver.from_settings(settings), transport=transport)

    def _send(self, request: HttpRequest) -> Any:
        if is_placeholder(self._organization) or is_placeholder(self._project):
            not_implemented(QUESTION, "ado organization/project in scope")
        if any(is_placeholder(path) for path in self._area_paths):
            not_implemented(QUESTION, "ado area paths in scope (a placeholder never widens to the whole project)")
        return self._transport.send(request)

    def _normalize(self, raw: Mapping[str, Any]) -> Signal:
        return normalize_ado_work_item(raw, redactor=self._redactor, organization=self._organization,
                                       project=self._project, entities=self._entities, ctx=self._ctx)

    def _batch(self, ids: Sequence[int]) -> list[Mapping[str, Any]]:
        """``workitemsbatch`` in chunks of 200; ``null`` entries (``errorPolicy=Omit``, deleted) are dropped."""
        items: list[Mapping[str, Any]] = []
        for start in range(0, len(ids), MAX_BATCH):
            values = as_mapping(self._send(self._api.workitems_batch(ids[start:start + MAX_BATCH]))).get("value")
            items += [item for item in values if isinstance(item, Mapping)] if isinstance(values, list) else []
        return items

    def poll_records(self, cursor: str | None, limit: int = 100) -> PollResult:
        """WIQL delta since *cursor* (an ISO timestamp) → batch read; the next cursor is the newest ChangedDate."""
        size = checked_limit(limit)
        since = require_timestamp(cursor) if cursor else None
        wiql = self._send(self._api.wiql_changed_since(since, top=size, area_paths=self._area_paths))
        ids = [int(item["id"]) for item in (wiql.get("workItems") or [])][:size]
        items = self._batch(ids) if ids else []
        return PollResult(source=self.source, signals=[self._normalize(item) for item in items],
                          next_cursor=newest_timestamp(
                              [as_mapping(item.get("fields")).get("System.ChangedDate") for item in items]) or cursor)

    def poll(self, cursor: str | None, limit: int = 100) -> PollResult:
        records = self.poll_records(cursor, limit)
        signals: list[Signal] = []
        for parent in records.signals:
            signals += [parent, *self._comments_for(parent)]
        return PollResult(source=self.source, signals=signals, next_cursor=records.next_cursor)

    def get(self, record_id: str) -> Signal | None:
        """Re-read one work item: ``None`` when *record_id* cannot be a work item id or the item no longer exists."""
        work_item_id = parse_work_item_id(record_id)
        if work_item_id is None:
            return None
        items = self._batch([work_item_id])
        return self._normalize(items[0]) if items else None

    def _comments_for(self, parent: Signal) -> list[Signal]:
        response = self._send(self._api.comments(parent.source_record_id))
        return [normalize_ado_comment(c, redactor=self._redactor, parent=parent, ctx=self._ctx)
                for c in response.get("comments") or []]

    def comments(self, work_item_id: str) -> list[Signal]:
        parent = self.get(str(work_item_id))
        return self._comments_for(parent) if parent is not None else []

    def search(self, query: str, top: int = 10) -> list[Signal]:
        result = self._send(self._api.wiql_search(query, top=checked_limit(top)))
        ids = [int(item["id"]) for item in (result.get("workItems") or [])][:top]
        return [self._normalize(item) for item in self._batch(ids)] if ids else []
