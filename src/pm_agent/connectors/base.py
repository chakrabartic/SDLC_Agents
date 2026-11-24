"""Connector contract (docs/09 §1) shared by read MCP servers and the ingestion workflow.

Every source connector returns **redacted**, canonical ``Signal``s with resolvable permalinks; vendor failures are
typed (never empty successes, IR3). ``make_signal`` is the single normalization path: NFC → redaction (secrets and
PII never leave the connector) → cross-reference extraction → deterministic ids (idempotent re-ingestion).
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Protocol

from pm_agent.grounding.dedup import extract_cross_refs
from pm_agent.grounding.redaction import Redactor
from pm_agent.schemas import (
    Customer,
    IngestionInfo,
    PersonRef,
    RecordType,
    RedactionSummary,
    Sensitivity,
    Signal,
    SignalTypeHint,
    SourceSystem,
    sha256_hex,
    utcnow,
)


class ConnectorStatus(StrEnum):
    OK = "ok"
    UNAVAILABLE = "unavailable"
    AUTH_FAILED = "auth_failed"
    RATE_LIMITED = "rate_limited"


class ConnectorError(Exception):
    """Typed vendor failure: unavailable, auth_failed, rate_limited, not_found or validation_failed."""

    def __init__(self, code: str, message: str = "", retry_after_seconds: float | None = None) -> None:
        super().__init__(f"{code}: {message}" if message else code)
        self.code = code
        self.retry_after_seconds = retry_after_seconds


@dataclass(frozen=True)
class PollResult:
    source: SourceSystem
    signals: list[Signal] = field(default_factory=list)
    next_cursor: str | None = None
    status: ConnectorStatus = ConnectorStatus.OK
    reason: str | None = None


class SourceConnector(Protocol):
    """One source system. ``poll`` returns changes after *cursor*; ``get`` re-reads one record (webhook hints)."""

    source: SourceSystem

    def poll(self, cursor: str | None, limit: int = 100) -> PollResult: ...

    def get(self, record_id: str) -> Signal | None: ...


def deterministic_signal_id(source_system: SourceSystem, source_record_id: str, source_version: str) -> str:
    """Same record version ⇒ same id, so re-ingestion is idempotent (docs/10 §2.1)."""
    return "sig_" + sha256_hex(f"{source_system.value}|{source_record_id}|{source_version}")[:26]


def make_signal(*, redactor: Redactor,  # pylint: disable=too-many-locals
                source_system: SourceSystem, source_record_id: str, source_version: str,
                record_type: RecordType, permalink: str, raw_text: str, captured_utc: datetime | None = None,
                title: str | None = None, author: PersonRef | None = None, customer: Customer | None = None,
                product: str | None = None, parent_signal_id: str | None = None,
                sensitivity: Sensitivity | None = None, type_hint: SignalTypeHint = SignalTypeHint.UNKNOWN,
                connector: str = "unknown", connector_version: str = "0", run_id: str = "adhoc",
                policy_version: str | None = None) -> Signal:
    """The only way connectors build ``Signal``s: normalized, redacted, cross-referenced, provenance-hashed."""
    text = redactor.redact(unicodedata.normalize("NFC", raw_text))
    safe_title = redactor.redact(unicodedata.normalize("NFC", title)).text if title else None
    return Signal(
        signal_id=deterministic_signal_id(source_system, source_record_id, source_version),
        source_system=source_system, source_record_id=source_record_id, source_version=source_version,
        record_type=record_type, parent_signal_id=parent_signal_id, permalink=permalink,
        captured_utc=captured_utc or utcnow(), author=author, customer=customer or Customer(), product=product,
        title=safe_title, raw_text=text.text, cross_refs=extract_cross_refs(text.text), signal_type_hint=type_hint,
        sensitivity=sensitivity or Sensitivity(),
        redaction=RedactionSummary(policy_version=policy_version or getattr(redactor, "policy_version", "unknown"),
                                   findings=text.findings),
        ingestion=IngestionInfo(run_id=run_id, connector=connector, connector_version=connector_version,
                                fetched_utc=utcnow()))
