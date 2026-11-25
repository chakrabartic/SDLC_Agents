"""Figma connector (``mcp-figma`` reads, ``mcp-figma-write`` comment shape, ``dls-catalog-sync``) — docs/09 §6.

The Figma REST API can *read* the DLS library (team components / component sets, file nodes, images) and post
comments; it cannot create frames or instances (verified), so rendering goes through the private plugin (ADR-006).

* ``DlsComponent`` — catalog entry (Figma key, logical ``dls_key`` such as ``dls:button/primary``, variants text).
* Comments → ``figma_comment`` signals (permalink ``https://www.figma.com/design/{file}?node-id=…#{comment}``;
  replies point at their parent comment).
* ``FigmaApi`` request builders; ``FixtureFigmaConnector``; ``HttpFigmaConnector`` (plan access token / OAuth app,
  team and library keys are Q-02/Q-03).

Open item: ``PLACEHOLDER:Q-02:figma_access_tokens`` (plan access token + OAuth ``file_comments:write``).
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Literal
from urllib.parse import quote

from pm_agent.connectors.base import ConnectorError, PollResult, deterministic_signal_id, make_signal
from pm_agent.connectors.common import (
    as_mapping,
    FixtureConnectorBase,
    FixtureUnit,
    IngestContext,
    context_or,
    default_fixture_root,
    first_text,
    normalize_text,
    checked_limit,
    parse_utc,
    person_ref,
    rank,
)
from pm_agent.connectors.http import HttpRequest, Transport, UnconfiguredTransport
from pm_agent.grounding.redaction import Redactor
from pm_agent.placeholders import is_placeholder, not_implemented
from pm_agent.schemas import RecordType, Signal, SourceSystem
from pm_agent.settings import Settings

FIGMA_API = "https://api.figma.com"
FIGMA_WEB = "https://www.figma.com"
QUESTION = "Q-02"
CONNECTOR = "mcp-figma"
IMAGE_FORMATS = frozenset({"png", "jpg", "svg", "pdf"})
ComponentKind = Literal["component", "component_set"]


@dataclass(frozen=True)
class DlsComponent:
    key: str
    name: str
    dls_key: str
    kind: ComponentKind
    description: str
    file_key: str
    node_id: str
    containing_frame: str | None = None
    page: str | None = None
    updated_at: str | None = None
    thumbnail_url: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def dls_key_for(name: str) -> str:
    """Logical DLS key used by ``PrototypeSpec`` (``Button/Primary`` → ``dls:button/primary``)."""
    return "dls:" + "/".join("-".join(part.strip().lower().split()) for part in name.split("/"))


def normalize_figma_component(raw: Mapping[str, Any], *, kind: ComponentKind = "component") -> DlsComponent:
    """One ``/v1/teams/{id}/components|component_sets`` entry → ``DlsComponent``."""
    key, name = first_text(raw.get("key")), first_text(raw.get("name"))
    if not key or not name:
        raise ConnectorError("validation_failed", "component without key/name")
    frame = as_mapping(raw.get("containing_frame"))
    return DlsComponent(key=key, name=name, dls_key=dls_key_for(name), kind=kind,
                        description=first_text(raw.get("description")), file_key=first_text(raw.get("file_key")),
                        node_id=first_text(raw.get("node_id")), containing_frame=first_text(frame.get("name")) or None,
                        page=first_text(frame.get("pageName")) or None,
                        updated_at=first_text(raw.get("updated_at")) or None,
                        thumbnail_url=first_text(raw.get("thumbnail_url")) or None)


def figma_node_permalink(file_key: str, node_id: str) -> str:
    return f"{FIGMA_WEB}/design/{quote(file_key, safe='')}?node-id={quote(node_id.replace(':', '-'), safe='-')}"


def figma_comment_permalink(file_key: str, comment_id: str, node_id: str | None = None) -> str:
    base = figma_node_permalink(file_key, node_id) if node_id else f"{FIGMA_WEB}/design/{quote(file_key, safe='')}"
    return f"{base}#{quote(comment_id, safe='')}"


def normalize_figma_comment(raw: Mapping[str, Any], *, redactor: Redactor, file_key: str | None = None,
                            ctx: IngestContext | None = None) -> Signal:
    """One file comment → ``figma_comment`` signal (design feedback loop, FR-B5)."""
    comment_id = first_text(raw.get("id"))
    key = file_key or first_text(raw.get("file_key"))
    if not comment_id or not key:
        raise ConnectorError("validation_failed", "comment without id/file_key")
    meta = as_mapping(raw.get("client_meta"))
    user = as_mapping(raw.get("user"))
    parent_id = first_text(raw.get("parent_id"))
    return make_signal(
        redactor=redactor, source_system=SourceSystem.FIGMA, source_record_id=comment_id, source_version="1",
        record_type=RecordType.FIGMA_COMMENT,
        permalink=figma_comment_permalink(key, comment_id, first_text(meta.get("node_id")) or None),
        raw_text=normalize_text(first_text(raw.get("message"))), captured_utc=parse_utc(raw.get("created_at")),
        author=person_ref(redactor, first_text(user.get("id"), user.get("handle")) or None),
        parent_signal_id=deterministic_signal_id(SourceSystem.FIGMA, parent_id, "1") if parent_id else None,
        **context_or(ctx, CONNECTOR).stamp())


class FigmaApi:
    """Pure request builders for the Figma REST API (OpenAPI v0.44.0 shapes, docs/09 §6)."""

    def __init__(self, base_url: str = FIGMA_API) -> None:
        self._base = base_url.rstrip("/")

    def _get(self, path: str, params: Sequence[tuple[str, str]] = ()) -> HttpRequest:
        return HttpRequest(method="GET", url=f"{self._base}{path}", params=tuple(params))

    @staticmethod
    def _paging(page_size_: int, after: int | None) -> list[tuple[str, str]]:
        params = [("page_size", str(page_size_))]
        if after is not None:
            params.append(("after", str(after)))
        return params

    def team_components(self, team_id: str, *, page_size: int = 30, after: int | None = None) -> HttpRequest:
        return self._get(f"/v1/teams/{quote(team_id, safe='')}/components", self._paging(page_size, after))

    def team_component_sets(self, team_id: str, *, page_size: int = 30, after: int | None = None) -> HttpRequest:
        return self._get(f"/v1/teams/{quote(team_id, safe='')}/component_sets", self._paging(page_size, after))

    def file_components(self, file_key: str) -> HttpRequest:
        return self._get(f"/v1/files/{quote(file_key, safe='')}/components")

    def component(self, key: str) -> HttpRequest:
        return self._get(f"/v1/components/{quote(key, safe='')}")

    def component_set(self, key: str) -> HttpRequest:
        return self._get(f"/v1/component_sets/{quote(key, safe='')}")

    def file_nodes(self, file_key: str, ids: Sequence[str], *, depth: int | None = None) -> HttpRequest:
        params = [("ids", ",".join(ids))] + ([("depth", str(depth))] if depth is not None else [])
        return self._get(f"/v1/files/{quote(file_key, safe='')}/nodes", params)

    def images(self, file_key: str, ids: Sequence[str], *, fmt: str = "png", scale: float = 2) -> HttpRequest:
        if fmt not in IMAGE_FORMATS:
            raise ValueError(f"image format must be one of {sorted(IMAGE_FORMATS)}")
        if not 0.01 <= scale <= 4:
            raise ValueError("scale must be between 0.01 and 4")
        return self._get(f"/v1/images/{quote(file_key, safe='')}",
                         [("ids", ",".join(ids)), ("format", fmt), ("scale", f"{scale:g}")])

    def comments(self, file_key: str) -> HttpRequest:
        return self._get(f"/v1/files/{quote(file_key, safe='')}/comments")

    def post_comment(self, file_key: str, message: str, *, node_id: str | None = None,
                     node_offset: tuple[float, float] = (0, 0)) -> HttpRequest:
        """``POST /v1/files/{key}/comments`` (OAuth ``file_comments:write``) — the FIGMA_POST_COMMENT write."""
        body: dict[str, Any] = {"message": message}
        if node_id:
            body["client_meta"] = {"node_id": node_id, "node_offset": {"x": node_offset[0], "y": node_offset[1]}}
        return HttpRequest(method="POST", url=f"{self._base}/v1/files/{quote(file_key, safe='')}/comments",
                           headers={"Content-Type": "application/json"}, json_body=body)


def _redact_json(value: Any, redactor: Redactor) -> Any:
    """Recursively redact every string leaf in a Figma node tree so text layers cannot leak secrets/PII (DM-5)."""
    if isinstance(value, str):
        return redactor.redact(value).text
    if isinstance(value, Mapping):
        return {k: _redact_json(v, redactor) for k, v in value.items()}
    if isinstance(value, list):
        return [_redact_json(item, redactor) for item in value]
    return value


class FixtureFigmaConnector(FixtureConnectorBase):
    """DLS library + prototype comments over synthetic Figma JSON (``tests/fixtures/connectors/figma``)."""

    def __init__(self, redactor: Redactor, *, path: Path | None = None, run_id: str = "fixture") -> None:
        data = json.loads((path or default_fixture_root() / "figma" / "library.json").read_text(encoding="utf-8"))
        ctx = IngestContext(connector="fixture-figma", run_id=run_id)
        self._redactor = redactor
        self._components = [normalize_figma_component(c) for c in data["components"]["meta"]["components"]]
        self._sets = [normalize_figma_component(c, kind="component_set")
                      for c in data["component_sets"]["meta"]["component_sets"]]
        self._nodes: dict[str, Any] = data.get("file_nodes") or {}
        comments = [normalize_figma_comment(c, redactor=redactor, ctx=ctx)
                    for c in (data.get("comments") or {}).get("comments") or []]
        super().__init__(SourceSystem.FIGMA, [FixtureUnit(parent=c) for c in comments])

    def components(self) -> list[DlsComponent]:
        return list(self._components)

    def component_sets(self) -> list[DlsComponent]:
        return list(self._sets)

    def catalog(self) -> list[DlsComponent]:
        return [*self._components, *self._sets]

    def search_dls(self, query: str, top: int = 10) -> list[DlsComponent]:
        return rank(query, ((c, f"{c.name} {c.description} {c.containing_frame or ''} {c.page or ''}")
                            for c in self.catalog()), top)

    def get_component(self, key: str) -> DlsComponent | None:
        return next((c for c in self.catalog() if key in (c.key, c.dls_key)), None)

    def file_nodes(self, file_key: str, ids: Sequence[str]) -> dict[str, Any]:
        del file_key
        nodes = self._nodes.get("nodes") or {}
        payload = {"name": self._nodes.get("name"), "lastModified": self._nodes.get("lastModified"),
                   "version": self._nodes.get("version"), "nodes": {i: nodes[i] for i in ids if i in nodes}}
        return dict(_redact_json(payload, self._redactor))

    def render_images(self, file_key: str, ids: Sequence[str], *, fmt: str = "png", scale: float = 2
                      ) -> dict[str, str]:
        FigmaApi().images(file_key, ids, fmt=fmt, scale=scale)  # same validation as the real call
        return {i: f"https://figma-images.example.invalid/{quote(file_key, safe='')}/{i.replace(':', '-')}.{fmt}"
                for i in ids}


class HttpFigmaConnector:
    """Figma REST client: plan access token for library reads, OAuth app for comments (Q-02 / Q-03)."""

    source = SourceSystem.FIGMA

    def __init__(self, *, team_id: str, file_keys: Sequence[str], redactor: Redactor,
                 transport: Transport | None = None, run_id: str = "adhoc") -> None:
        self._team_id = team_id
        self._file_keys = list(file_keys)
        self._api = FigmaApi()
        self._redactor = redactor
        self._transport = transport or UnconfiguredTransport("Q-02", "figma")
        self._ctx = IngestContext(connector=CONNECTOR, run_id=run_id)

    @classmethod
    def from_settings(cls, settings: Settings, redactor: Redactor, *,
                      transport: Transport | None = None) -> "HttpFigmaConnector":
        """Team id and DLS library key from ``connectors.figma`` (placeholders until Q-02/Q-03)."""
        cfg = settings.connectors.get("figma") or {}
        team = str(cfg.get("team_id") or "")
        library = str(cfg.get("dls_library_file_key") or "")
        if settings.strict and (is_placeholder(team) or is_placeholder(library)):
            not_implemented("Q-02", "figma team id and DLS library file key")
        return cls(team_id=team, file_keys=[library] if library else [], redactor=redactor, transport=transport)

    def _send(self, request: HttpRequest) -> Any:
        if is_placeholder(self._team_id) or any(is_placeholder(k) for k in self._file_keys):
            not_implemented("Q-02", "figma team id, DLS library file key and access tokens")
        return self._transport.send(request)

    def components(self) -> list[DlsComponent]:
        response = self._send(self._api.team_components(self._team_id, page_size=100))
        return [normalize_figma_component(c) for c in (response.get("meta") or {}).get("components") or []]

    def component_sets(self) -> list[DlsComponent]:
        response = self._send(self._api.team_component_sets(self._team_id, page_size=100))
        return [normalize_figma_component(c, kind="component_set")
                for c in (response.get("meta") or {}).get("component_sets") or []]

    def catalog(self) -> list[DlsComponent]:
        return [*self.components(), *self.component_sets()]

    def search_dls(self, query: str, top: int = 10) -> list[DlsComponent]:
        """Searches a live catalog read (production: the ``config.dls_catalog`` snapshot from dls-catalog-sync)."""
        return rank(query, ((c, f"{c.name} {c.description}") for c in self.catalog()), top)

    def get_component(self, key: str) -> DlsComponent | None:
        """``GET /v1/components/{key}``; ``None`` when Figma answers 404."""
        try:
            response = self._send(self._api.component(key))
        except ConnectorError as exc:
            if exc.code == "not_found":
                return None
            raise
        meta = response.get("meta") if isinstance(response, Mapping) else None
        return normalize_figma_component(meta) if isinstance(meta, Mapping) else None

    def file_nodes(self, file_key: str, ids: Sequence[str]) -> dict[str, Any]:
        return dict(_redact_json(dict(self._send(self._api.file_nodes(file_key, ids))), self._redactor))

    def render_images(self, file_key: str, ids: Sequence[str], *, fmt: str = "png", scale: float = 2
                      ) -> dict[str, str]:
        response = self._send(self._api.images(file_key, ids, fmt=fmt, scale=scale))
        images = response.get("images") or {}
        return {str(k): str(v) for k, v in images.items() if v}

    def poll(self, cursor: str | None, limit: int = 100) -> PollResult:
        """Comments on the configured files (comment endpoints are not paged; *cursor* is passed through)."""
        size = checked_limit(limit)
        signals: list[Signal] = []
        for file_key in self._file_keys:
            response = self._send(self._api.comments(file_key))
            signals += [normalize_figma_comment(c, redactor=self._redactor, file_key=file_key, ctx=self._ctx)
                        for c in (response.get("comments") or [])[:size]]
        return PollResult(source=self.source, signals=signals, next_cursor=cursor)

    def get(self, record_id: str) -> Signal | None:
        return next((s for s in self.poll(None, limit=200).signals if s.source_record_id == record_id), None)
