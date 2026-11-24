"""Evidence entities: canonical ``Signal`` and citable ``EvidenceChunk`` (docs/10 §2)."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from pm_agent.schemas.canonical import new_id, sha256_hex


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)


class SourceSystem(StrEnum):
    ADO = "ado"
    SERVICENOW = "servicenow"
    PAGERDUTY = "pagerduty"
    EMAIL = "email"
    ONEDRIVE = "onedrive"
    PM_INPUT = "pm_input"
    KB = "kb"
    FIGMA = "figma"
    # extension channels (requirements §8)
    TEAMS = "teams"
    USERVOICE = "uservoice"
    USERSNAP = "usersnap"
    CSM = "csm"
    ESM = "esm"
    MYELITE = "myelite"
    GRAFANA = "grafana"


class RecordType(StrEnum):
    INCIDENT = "incident"
    REQUEST = "request"
    CASE = "case"
    WORK_ITEM = "work_item"
    COMMENT = "comment"
    JOURNAL_NOTE = "journal_note"
    EMAIL_MESSAGE = "email_message"
    DOCUMENT = "document"
    TRACKER_ROW = "tracker_row"
    PM_ANSWER = "pm_answer"
    KB_ARTICLE = "kb_article"
    FIGMA_COMMENT = "figma_comment"


class SignalTypeHint(StrEnum):
    FEATURE_REQUEST = "feature_request"
    WORKFLOW_PROBLEM = "workflow_problem"
    KNOWLEDGE_GAP = "knowledge_gap"
    DEFECT = "defect"
    ENHANCEMENT = "enhancement"
    UNKNOWN = "unknown"


class SignalStatus(StrEnum):
    ACTIVE = "active"
    SUPERSEDED = "superseded"
    TOMBSTONED = "tombstoned"


class PersonRef(_Model):
    """Pseudonymous person reference — real names never enter prompts, drafts or logs (DM-5)."""

    token: str
    role_hint: str | None = None
    org_unit: str | None = None


class Customer(_Model):
    name: str | None = None
    region: str | None = None


class ArtifactHandle(_Model):
    """Large payloads are stored by reference (IR4)."""

    uri: str
    mime_type: str
    sha256: str
    size_bytes: int = Field(ge=0)


class CrossRef(_Model):
    """Deterministic identifier found in text (e.g. an ADO URL inside a ServiceNow note) — a hard link."""

    system: SourceSystem
    record_id: str
    pattern_id: str
    char_start: int = Field(ge=0)
    char_end: int = Field(ge=0)


class Sensitivity(_Model):
    purview_label: str | None = None
    legal_tags: list[str] = Field(default_factory=list)


class RedactionSummary(_Model):
    policy_version: str
    findings: dict[str, int] = Field(default_factory=dict)  # counts only, never values


class IngestionInfo(_Model):
    run_id: str
    connector: str
    connector_version: str
    fetched_utc: datetime


def compute_provenance_hash(source_system: str, source_record_id: str, source_version: str, raw_text: str) -> str:
    """sha256(source_system|source_record_id|source_version|sha256(raw_text)) — docs/10 §2.1."""
    return sha256_hex("|".join([source_system, source_record_id, source_version, sha256_hex(raw_text)]))


class Signal(_Model):
    """One normalized, **redacted** source record version (IR2: immutable provenance)."""

    signal_id: str = Field(default_factory=lambda: new_id("sig"))
    source_system: SourceSystem
    source_record_id: str = Field(min_length=1)
    source_version: str = Field(min_length=1)
    record_type: RecordType
    parent_signal_id: str | None = None
    permalink: str
    captured_utc: datetime
    author: PersonRef | None = None
    customer: Customer = Field(default_factory=Customer)
    product: str | None = None
    title: str | None = None
    raw_text: str
    artifacts: list[ArtifactHandle] = Field(default_factory=list)
    cross_refs: list[CrossRef] = Field(default_factory=list)
    signal_type_hint: SignalTypeHint = SignalTypeHint.UNKNOWN
    language: str = "en"
    sensitivity: Sensitivity = Field(default_factory=Sensitivity)
    redaction: RedactionSummary | None = None
    ingestion: IngestionInfo | None = None
    provenance_hash: str | None = None
    status: SignalStatus = SignalStatus.ACTIVE

    @field_validator("permalink")
    @classmethod
    def _permalink_resolvable(cls, value: str) -> str:
        if not value.startswith(("https://", "http://", "pm-agent://")):
            raise ValueError("permalink must be a resolvable https:// (or pm-agent:// for PM input) link")
        return value

    @model_validator(mode="after")
    def _provenance(self) -> "Signal":
        expected = compute_provenance_hash(self.source_system.value, self.source_record_id,
                                           self.source_version, self.raw_text)
        if self.provenance_hash is None:
            object.__setattr__(self, "provenance_hash", expected)
        elif self.provenance_hash != expected:
            raise ValueError("provenance_hash does not match the signal content (tampered or stale record)")
        return self


class EvidenceChunk(_Model):
    """The citable unit: a slice of ``Signal.raw_text`` (only ``evidence_id`` may be cited)."""

    evidence_id: str = Field(default_factory=lambda: new_id("ev"))
    signal_id: str
    seq: int = Field(ge=0)
    text: str = Field(min_length=1)
    char_start: int = Field(ge=0)
    char_end: int = Field(ge=0)
    text_sha256: str | None = None
    embedding: list[float] | None = None

    @model_validator(mode="after")
    def _consistent(self) -> "EvidenceChunk":
        if self.char_end - self.char_start != len(self.text):
            raise ValueError("char_end - char_start must equal len(text)")
        digest = sha256_hex(self.text)
        if self.text_sha256 is None:
            object.__setattr__(self, "text_sha256", digest)
        elif self.text_sha256 != digest:
            raise ValueError("text_sha256 does not match text")
        return self
