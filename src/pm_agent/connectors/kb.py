"""SLB Knowledge Base adapter (``mcp-kb`` / ``mcp-kb-write``) — docs/09 §7.

No public SLB KB API is known (Q-01), so the servers code against a backend-agnostic ``KbAdapter``::

    capabilities() -> {native_draft, approval_workflow, versioning, labels}
    search(query, filters, top) -> [KbHit]            # read server
    get(article_id) -> KbArticle                       # read server
    validate(draft) -> ValidationResult                # write server (pre-flight)
    create_draft(draft) -> ExternalRef                 # write server
    publish(ref) -> ExternalRef | PublishRequiresWorkflow
    retire(ref)

``FixtureKbAdapter`` is the in-memory reference (local/dev and tests); ``HttpKbAdapter`` is the placeholder for the
real backend (ServiceNow Knowledge, Confluence, SharePoint pages or ADO Wiki — candidates in docs/09 §7).
"""

from __future__ import annotations

import itertools
import json
import threading
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, NoReturn, Protocol, TypedDict

from pm_agent.connectors.base import make_signal
from pm_agent.connectors.common import (
    IngestContext,
    context_or,
    default_fixture_root,
    normalize_text,
    parse_utc,
    rank,
)
from pm_agent.connectors.entities import EntityResolver, resolve_customer, resolve_product
from pm_agent.grounding.redaction import Redactor
from pm_agent.placeholders import not_implemented
from pm_agent.schemas import RecordType, Signal, SourceSystem, utcnow
from pm_agent.settings import Settings

QUESTION = "Q-01"
CONNECTOR = "mcp-kb"
MAX_TITLE_CHARS = 255
MAX_BODY_CHARS = 100_000


class KbCapabilities(TypedDict):
    native_draft: bool
    approval_workflow: bool
    versioning: bool
    labels: bool


@dataclass(frozen=True)
class KbHit:
    article_id: str
    title: str
    url: str
    score: float
    snippet: str

    def as_dict(self) -> dict[str, Any]:
        return {"article_id": self.article_id, "title": self.title, "url": self.url, "score": self.score,
                "snippet": self.snippet}


@dataclass(frozen=True)
class KbArticle:
    article_id: str
    version: str
    title: str
    body: str
    url: str
    labels: tuple[str, ...] = ()
    state: str = "published"
    updated_utc: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {"article_id": self.article_id, "version": self.version, "title": self.title, "body": self.body,
                "url": self.url, "labels": list(self.labels), "state": self.state, "updated_utc": self.updated_utc}


@dataclass(frozen=True)
class ValidationResult:
    ok: bool
    problems: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class ExternalRef:
    system: str
    external_id: str
    url: str
    version: str
    state: str

    def as_dict(self) -> dict[str, str]:
        return {"system": self.system, "external_id": self.external_id, "url": self.url, "version": self.version,
                "state": self.state}


@dataclass(frozen=True)
class PublishRequiresWorkflow:
    """The backend has its own approval workflow: the draft was submitted; the ledger records the backend's event."""

    ref: ExternalRef
    workflow: str


class KbAdapter(Protocol):
    def capabilities(self) -> KbCapabilities: ...

    def search(self, query: str, filters: Mapping[str, str] | None = None, top: int = 10) -> list[KbHit]: ...

    def get(self, article_id: str) -> KbArticle: ...

    def validate(self, draft: Mapping[str, Any]) -> ValidationResult: ...

    def create_draft(self, draft: Mapping[str, Any]) -> ExternalRef: ...

    def publish(self, ref: ExternalRef) -> ExternalRef | PublishRequiresWorkflow: ...

    def retire(self, ref: ExternalRef) -> None: ...


def validate_draft(draft: Mapping[str, Any]) -> ValidationResult:
    """Backend-independent pre-flight: a title and a body within size limits."""
    problems = []
    title = str(draft.get("title") or "").strip()
    body = str(draft.get("body") or "").strip()
    if not title:
        problems.append("title is required")
    elif len(title) > MAX_TITLE_CHARS:
        problems.append(f"title exceeds {MAX_TITLE_CHARS} characters")
    if not body:
        problems.append("body is required")
    elif len(body) > MAX_BODY_CHARS:
        problems.append(f"body exceeds {MAX_BODY_CHARS} characters")
    return ValidationResult(ok=not problems, problems=problems)


def normalize_kb_article(article: KbArticle, *, redactor: Redactor, entities: EntityResolver | None = None,
                         ctx: IngestContext | None = None) -> Signal:
    """A KB article version → ``kb_article`` signal (prior-art evidence, citable like any other source)."""
    text = normalize_text(article.body)
    return make_signal(
        redactor=redactor, source_system=SourceSystem.KB, source_record_id=article.article_id,
        source_version=article.version, record_type=RecordType.KB_ARTICLE, permalink=article.url,
        raw_text=text or article.title, title=article.title, captured_utc=parse_utc(article.updated_utc),
        customer=resolve_customer(entities, None, article.title, text),
        product=resolve_product(entities, article.title, text), **context_or(ctx, CONNECTOR).stamp())


class FixtureKbAdapter:
    """In-memory KB over synthetic articles; ``approval_workflow=True`` mimics a backend with its own review."""

    def __init__(self, path: Path | None = None, *, approval_workflow: bool = False) -> None:
        data = json.loads((path or default_fixture_root() / "kb" / "articles.json").read_text(encoding="utf-8"))
        self._base = str((data.get("_meta") or {}).get("base_url") or "https://kb.example.invalid").rstrip("/")
        self._workflow = approval_workflow
        self._articles = {a["id"]: KbArticle(article_id=a["id"], version=str(a.get("version") or "1"),
                                             title=a["title"], body=a["body"], url=a["url"],
                                             labels=tuple(a.get("labels") or []), state=a.get("state", "published"),
                                             updated_utc=a.get("updated_utc"))
                          for a in data.get("articles") or []}
        self._ids = itertools.count(9_000_001)
        self._lock = threading.Lock()

    def capabilities(self) -> KbCapabilities:
        return {"native_draft": True, "approval_workflow": self._workflow, "versioning": True, "labels": True}

    def search(self, query: str, filters: Mapping[str, str] | None = None, top: int = 10) -> list[KbHit]:
        """Lexical search over non-retired articles; ``filters`` match ``labels`` / ``state`` exactly."""
        wanted = dict(filters or {})
        with self._lock:
            candidates = [a for a in self._articles.values() if a.state != "retired"
                          and ("labels" not in wanted or wanted["labels"] in a.labels)
                          and ("state" not in wanted or wanted["state"] == a.state)]
        hits = rank(query, ((a, f"{a.title} {a.body} {' '.join(a.labels)}") for a in candidates), top)
        total = max(len(query.split()), 1)
        return [KbHit(article_id=a.article_id, title=a.title, url=a.url,
                      score=round(len(set(query.lower().split()) & set(f"{a.title} {a.body}".lower().split()))
                                  / total, 4), snippet=a.body[:200]) for a in hits]

    def get(self, article_id: str) -> KbArticle:
        with self._lock:
            try:
                return self._articles[article_id]
            except KeyError:
                raise LookupError(f"unknown KB article {article_id}") from None

    def validate(self, draft: Mapping[str, Any]) -> ValidationResult:
        return validate_draft(draft)

    def _ref(self, article: KbArticle) -> ExternalRef:
        return ExternalRef(system="kb", external_id=article.article_id, url=article.url, version=article.version,
                           state=article.state)

    def create_draft(self, draft: Mapping[str, Any]) -> ExternalRef:
        """Store a validated draft (``state=draft``) and return its external reference."""
        checked = validate_draft(draft)
        if not checked.ok:
            raise ValueError("; ".join(checked.problems))
        with self._lock:
            article_id = f"KB{next(self._ids):07d}"
            article = KbArticle(article_id=article_id, version="1", title=str(draft["title"]).strip(),
                                body=str(draft["body"]).strip(), url=f"{self._base}/articles/{article_id}",
                                labels=tuple(str(label) for label in draft.get("labels") or []), state="draft",
                                updated_utc=utcnow().isoformat())
            self._articles[article_id] = article
        return self._ref(article)

    def _set_state(self, ref: ExternalRef, state: str) -> ExternalRef:
        with self._lock:
            article = replace(self._articles[ref.external_id], state=state, updated_utc=utcnow().isoformat())
            self._articles[ref.external_id] = article
        return self._ref(article)

    def publish(self, ref: ExternalRef) -> ExternalRef | PublishRequiresWorkflow:
        if self._workflow:
            return PublishRequiresWorkflow(ref=self._set_state(ref, "in_review"), workflow="backend-review")
        return self._set_state(ref, "published")

    def retire(self, ref: ExternalRef) -> None:
        self._set_state(ref, "retired")


class HttpKbAdapter:
    """Placeholder for the real SLB KB backend — every call names Q-01 until the backend is chosen."""

    def __init__(self, backend: str, base_url: str) -> None:
        self.backend = backend
        self.base_url = base_url

    @classmethod
    def from_settings(cls, settings: Settings) -> "HttpKbAdapter":
        cfg = settings.connectors.get("kb") or {}
        return cls(backend=str(cfg.get("backend") or ""), base_url=str(cfg.get("base_url") or ""))

    def _blocked(self, operation: str) -> NoReturn:
        not_implemented("Q-01", f"KB backend adapter {operation} (backend={self.backend!r})")

    def capabilities(self) -> KbCapabilities:
        self._blocked("capabilities")

    def search(self, query: str, filters: Mapping[str, str] | None = None, top: int = 10) -> list[KbHit]:
        del query, filters, top
        self._blocked("search")

    def get(self, article_id: str) -> KbArticle:
        del article_id
        self._blocked("get")

    def validate(self, draft: Mapping[str, Any]) -> ValidationResult:
        del draft
        self._blocked("validate")

    def create_draft(self, draft: Mapping[str, Any]) -> ExternalRef:
        del draft
        self._blocked("create_draft")

    def publish(self, ref: ExternalRef) -> ExternalRef | PublishRequiresWorkflow:
        del ref
        self._blocked("publish")

    def retire(self, ref: ExternalRef) -> None:
        del ref
        self._blocked("retire")
