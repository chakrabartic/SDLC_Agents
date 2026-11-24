"""The grounding contract: ``Claim`` + ``Citation`` (docs/10 §2.3, R6).

Two layers on purpose:
* **LLM-facing** ``ClaimDraft`` / ``CitedQuote`` — models are good at copying quotes, bad at character offsets.
* **Resolved** ``Claim`` / ``Citation`` — produced by the Verifier after locating the quote in the evidence chunk
  (span, hash, permalink filled by code, never by a model).
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, model_validator

from pm_agent.schemas.canonical import new_id, sha256_hex
from pm_agent.schemas.evidence import SourceSystem

MAX_QUOTE_CHARS = 400


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ClaimKind(StrEnum):
    FACT = "fact"
    CLASSIFICATION = "classification"
    AGGREGATE = "aggregate"
    PM_JUDGMENT = "pm_judgment"
    INFERENCE = "inference"
    PROCEDURAL = "procedural"


CITATION_REQUIRED: frozenset[ClaimKind] = frozenset(
    {ClaimKind.FACT, ClaimKind.CLASSIFICATION, ClaimKind.AGGREGATE, ClaimKind.PM_JUDGMENT})


class CitedQuote(_Model):
    """LLM output: which evidence chunk and the exact quote copied from it."""

    evidence_id: str
    quote: str = Field(min_length=1, max_length=MAX_QUOTE_CHARS)


class ClaimDraft(_Model):
    """LLM output for one atomic statement (verified before it can become a ``Claim``)."""

    text: str = Field(min_length=1)
    kind: ClaimKind
    citations: list[CitedQuote] = Field(default_factory=list)


class Span(_Model):
    start: int = Field(ge=0)
    end: int = Field(ge=1)

    @model_validator(mode="after")
    def _ordered(self) -> "Span":
        if self.end <= self.start:
            raise ValueError("span end must be greater than start")
        return self


class Citation(_Model):
    """A resolved citation: exact span inside an active evidence chunk, with its provenance."""

    evidence_id: str
    signal_id: str
    source_system: SourceSystem
    source_record_id: str
    permalink: str
    span: Span
    quote: str = Field(min_length=1, max_length=MAX_QUOTE_CHARS)
    quote_sha256: str | None = None

    @model_validator(mode="after")
    def _hash(self) -> "Citation":
        if self.span.end - self.span.start != len(self.quote):
            raise ValueError("span length must equal quote length")
        digest = sha256_hex(self.quote)
        if self.quote_sha256 is None:
            object.__setattr__(self, "quote_sha256", digest)
        elif self.quote_sha256 != digest:
            raise ValueError("quote_sha256 does not match quote")
        return self


class VerificationStatus(StrEnum):
    PENDING = "pending"
    PASS = "pass"
    FAIL = "fail"


class ClaimVerification(_Model):
    status: VerificationStatus = VerificationStatus.PENDING
    failed_checks: list[str] = Field(default_factory=list)


class Claim(_Model):
    """One atomic, verifiable statement."""

    claim_id: str = Field(default_factory=lambda: new_id("clm"))
    text: str = Field(min_length=1)
    kind: ClaimKind
    citations: list[Citation] = Field(default_factory=list)
    atoms: list[str] = Field(default_factory=list)
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    verification: ClaimVerification = Field(default_factory=ClaimVerification)

    @property
    def requires_citation(self) -> bool:
        """fact / classification / aggregate / pm_judgment must cite; inference & procedural cannot be facts."""
        return self.kind in CITATION_REQUIRED


class FieldStatus(StrEnum):
    POPULATED = "populated"
    PARTIAL = "partial"
    MISSING = "missing"
    INFERRED = "inferred"


class DraftField(_Model):
    """A drafted template field: always a list of claims (DM-1), never free text."""

    status: FieldStatus
    claims: list[Claim] = Field(default_factory=list)

    @model_validator(mode="after")
    def _status_matches_claims(self) -> "DraftField":
        if self.status is FieldStatus.MISSING and self.claims:
            raise ValueError("a missing field cannot carry claims")
        if self.status is not FieldStatus.MISSING and not self.claims:
            raise ValueError(f"a {self.status} field needs at least one claim")
        if self.status is FieldStatus.INFERRED and any(c.kind is not ClaimKind.INFERENCE for c in self.claims):
            raise ValueError("an inferred field may only contain inference claims")
        return self
