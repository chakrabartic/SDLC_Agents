"""Shared connector helpers (docs/09 §1): text normalization, pseudonymous people, timestamps, cursors, paging.

Everything here is deterministic and network-free. ``FixtureConnectorBase`` implements the ``SourceConnector``
paging contract over pre-normalized signals so every fixture connector behaves identically:

* ``poll(cursor, limit)`` returns up to *limit* source records **plus their child signals** (comments, journal
  entries, notes) in vendor delivery order; ``next_cursor`` is always a resume point (an empty page means "caught up").
* cursors are opaque, validated strings (``fx:<offset>``) — a tampered cursor is a typed ``validation_failed``.
"""

from __future__ import annotations

import os
import re
import unicodedata
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, TypedDict

from pm_agent.connectors.base import ConnectorError, PollResult
from pm_agent.grounding.redaction import Redactor
from pm_agent.schemas import PersonRef, Signal, SourceSystem, sha256_hex

CONNECTOR_VERSION = "1.0.0"
MAX_PAGE_SIZE = 200
FIXTURE_ROOT_ENV = "PM_AGENT_FIXTURE_ROOT"

_BLOCK_TAGS = frozenset({
    "address", "article", "aside", "blockquote", "br", "dd", "div", "dl", "dt", "footer", "h1", "h2", "h3", "h4",
    "h5", "h6", "header", "hr", "li", "ol", "p", "pre", "section", "table", "td", "th", "tr", "ul",
})
_SKIP_TAGS = frozenset({"head", "script", "style", "title"})
_SPACES = re.compile(r"[ \t\f\v\u00a0]+")
# ``fullmatch`` over ASCII classes: ``$`` also matches before a trailing newline and ``\d`` matches any Unicode digit.
_SNOW_DATETIME = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2} [0-9]{2}:[0-9]{2}:[0-9]{2}")
_ISO_FRACTION = re.compile(r"(?P<head>.+T[0-9]{2}:[0-9]{2}:[0-9]{2})\.(?P<frac>[0-9]+)(?P<tail>.*)")
_CURSOR = re.compile(r"fx:([0-9]{1,9})")
_WORD = re.compile(r"[a-z0-9_]+")


class Stamp(TypedDict):
    connector: str
    connector_version: str
    run_id: str


@dataclass(frozen=True)
class IngestContext:
    """Provenance stamped on every signal (``Signal.ingestion``)."""

    connector: str = "unknown"
    connector_version: str = CONNECTOR_VERSION
    run_id: str = "adhoc"

    def stamp(self) -> Stamp:
        return {"connector": self.connector, "connector_version": self.connector_version, "run_id": self.run_id}


def context_or(ctx: IngestContext | None, connector: str) -> IngestContext:
    return ctx if ctx is not None else IngestContext(connector=connector)


def as_mapping(value: object) -> Mapping[str, Any]:
    """*value* if it is a JSON object, else an empty mapping (vendor payloads omit or null nested objects)."""
    return value if isinstance(value, Mapping) else {}


# ------------------------------------------------------------------------------------------------- text
class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skipping = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        del attrs
        if tag in _SKIP_TAGS:
            self._skipping += 1
        elif tag in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _BLOCK_TAGS:
            self.parts.append("\n")
        del attrs

    def handle_endtag(self, tag: str) -> None:
        if tag in _SKIP_TAGS:
            self._skipping = max(0, self._skipping - 1)
        elif tag in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._skipping:
            self.parts.append(data)


def normalize_text(text: str) -> str:
    """NFC, unified newlines, collapsed runs of blanks, trimmed lines, no empty lines at the ends."""
    normalized = unicodedata.normalize("NFC", text).replace("\r\n", "\n").replace("\r", "\n")
    lines = [_SPACES.sub(" ", line).strip() for line in normalized.split("\n")]
    out: list[str] = []
    for line in lines:
        if line or (out and out[-1]):
            out.append(line)
    return "\n".join(out).strip()


def html_to_text(value: str) -> str:
    """Vendor HTML (ADO fields, Outlook bodies) → plain text: one line per block element, entities decoded."""
    parser = _TextExtractor()
    parser.feed(value)
    parser.close()
    lines = [_SPACES.sub(" ", line).strip() for line in "".join(parser.parts).replace("\r", "").split("\n")]
    return "\n".join(line for line in lines if line)


def text_of(content: str, content_type: str | None) -> str:
    return html_to_text(content) if (content_type or "").lower() == "html" else normalize_text(content)


# ------------------------------------------------------------------------------------------------ people
def person_ref(redactor: Redactor, identity: str | None, *, role_hint: str | None = None,
               org_unit: str | None = None) -> PersonRef | None:
    """Pseudonymous author reference from a vendor identity (email, user name, user id) — never the raw value.

    Uses the redactor's keyed pseudonym (``LocalRedactor.pseudonym``) when available, else its redaction (SDP
    surrogates). The unkeyed hash fallback only applies to identifiers SDP does not detect (e.g. opaque vendor ids).
    """
    if identity is None or not identity.strip():
        return None
    normalized = identity.strip().lower()
    pseudonym = getattr(redactor, "pseudonym", None)
    if callable(pseudonym):
        token = str(pseudonym("PERSON_MENTION", normalized))
    else:
        redacted = redactor.redact(normalized).text
        token = redacted if redacted != normalized and "@" not in redacted \
            else "PERSON_" + sha256_hex(normalized)[:6].upper()
    return PersonRef(token=token, role_hint=role_hint, org_unit=org_unit)


# -------------------------------------------------------------------------------------------- timestamps
def parse_utc(value: object) -> datetime | None:
    """ISO 8601 (``Z``/offsets, 7-digit ADO fractions) or ServiceNow ``YYYY-MM-DD HH:MM:SS`` (UTC) → aware UTC.

    ``None`` for anything else — including strings *shaped* like a timestamp that name no real instant
    (``2026-02-30 10:00:00``) or that leave the representable range when converted to UTC
    (``9999-12-31T23:59:59-23:59``). Never raises: cursors reach this function straight from MCP clients.
    """
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    try:
        if _SNOW_DATETIME.fullmatch(text):
            return datetime.strptime(text, "%Y-%m-%d %H:%M:%S").replace(tzinfo=UTC)
        match = _ISO_FRACTION.fullmatch(text)
        if match:
            text = f"{match.group('head')}.{match.group('frac')[:6]}{match.group('tail')}"
        parsed = datetime.fromisoformat(text)
        return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)
    except (ValueError, OverflowError):
        return None


def require_timestamp(cursor: str) -> str:
    """Delta cursors that end up inside vendor query languages must be timestamps — nothing else gets through."""
    if parse_utc(cursor) is None:
        raise ConnectorError("validation_failed", "cursor is not a timestamp")
    return cursor


def newest_timestamp(values: Iterable[object]) -> str | None:
    """The newest parseable timestamp among *values*, returned verbatim (it becomes the next delta cursor)."""
    stamped = [(parsed, value) for value in values
               if isinstance(value, str) and (parsed := parse_utc(value)) is not None]
    return max(stamped)[1] if stamped else None


# ----------------------------------------------------------------------------------------------- cursors
def encode_offset_cursor(offset: int) -> str:
    return f"fx:{offset}"


def decode_offset_cursor(cursor: str | None) -> int:
    """Offset of an ``fx:<n>`` cursor (``None`` = start); anything else is a typed ``validation_failed``."""
    if cursor is None or cursor == "":
        return 0
    match = _CURSOR.fullmatch(cursor)
    if match is None:
        raise ConnectorError("validation_failed", "unrecognized cursor")
    return int(match.group(1))


def checked_limit(limit: int) -> int:
    """Validate a requested page size and apply the server-side cap (docs/09 §1: servers enforce page caps)."""
    if limit < 1:
        raise ConnectorError("validation_failed", "limit must be at least 1")
    return min(limit, MAX_PAGE_SIZE)


# ------------------------------------------------------------------------------------------------ search
def terms(text: str) -> set[str]:
    return set(_WORD.findall(text.lower()))


def rank(query: str, items: Iterable[tuple[Any, str]], top: int) -> list[Any]:
    """Naive lexical ranking (stand-in for vendor search): term overlap, ties in input order."""
    wanted = terms(query)
    scored = []
    for index, (item, text) in enumerate(items):
        overlap = len(wanted & terms(text))
        if overlap:
            scored.append((-overlap, index, item))
    scored.sort(key=lambda entry: (entry[0], entry[1]))
    return [item for _, _, item in scored[:max(top, 0)]]


# ------------------------------------------------------------------------------------------------ paging
@dataclass(frozen=True)
class FixtureUnit:
    """One delivered source record: its signal, child signals and alternate lookup keys (sys_id, API id)."""

    parent: Signal
    children: tuple[Signal, ...] = ()
    aliases: frozenset[str] = frozenset()

    def matches(self, record_id: str) -> bool:
        return record_id == self.parent.source_record_id or record_id in self.aliases


class FixtureConnectorBase:
    """``SourceConnector`` over pre-normalized units with deterministic offset paging."""

    def __init__(self, source: SourceSystem, units: Sequence[FixtureUnit]) -> None:
        self.source = source
        self._units = list(units)

    def _window(self, cursor: str | None, limit: int) -> tuple[int, int]:
        size = checked_limit(limit)
        start = min(decode_offset_cursor(cursor), len(self._units))
        return start, min(start + size, len(self._units))

    def poll(self, cursor: str | None, limit: int = 100) -> PollResult:
        """Records after *cursor* with their children; the returned cursor resumes after the last record."""
        start, end = self._window(cursor, limit)
        signals = [signal for unit in self._units[start:end] for signal in (unit.parent, *unit.children)]
        return PollResult(source=self.source, signals=signals, next_cursor=encode_offset_cursor(end))

    def poll_records(self, cursor: str | None, limit: int = 100) -> PollResult:
        """Like ``poll`` but source records only (the read MCP servers' ``list_*`` delta tools)."""
        start, end = self._window(cursor, limit)
        return PollResult(source=self.source, signals=[unit.parent for unit in self._units[start:end]],
                          next_cursor=encode_offset_cursor(end))

    def _unit(self, record_id: str) -> FixtureUnit | None:
        return next((unit for unit in self._units if unit.matches(record_id)), None)

    def get(self, record_id: str) -> Signal | None:
        """Re-read one record (webhook hint): by record id or alias; child records are found too."""
        unit = self._unit(record_id)
        if unit is not None:
            return unit.parent
        return next((child for unit in self._units for child in unit.children
                     if child.source_record_id == record_id), None)

    def children(self, record_id: str) -> list[Signal]:
        unit = self._unit(record_id)
        return list(unit.children) if unit is not None else []

    def records(self) -> list[Signal]:
        """Every distinct source record (first delivery wins)."""
        seen: dict[str, Signal] = {}
        for unit in self._units:
            seen.setdefault(unit.parent.signal_id, unit.parent)
        return list(seen.values())

    def search(self, query: str, top: int = 10) -> list[Signal]:
        """Lexical search over titles and text of distinct records (prior-art lookups)."""
        return rank(query, ((s, f"{s.title or ''} {s.raw_text}") for s in self.records()), top)


def default_fixture_root() -> Path:
    """``$PM_AGENT_FIXTURE_ROOT`` or the repository's ``tests/fixtures/connectors`` (dev checkouts only)."""
    override = os.environ.get(FIXTURE_ROOT_ENV)
    if override:
        return Path(override)
    return Path(__file__).resolve().parents[3] / "tests" / "fixtures" / "connectors"


def first_text(*values: object) -> str:
    """The first non-empty string among *values* (vendor payloads spread one fact over several fields)."""
    for value in values:
        if isinstance(value, str) and value.strip():
            return value
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return str(value)
    return ""
