"""Subject identity — what verification reports, approvals and approval tokens bind to (docs/06 §3, docs/07 §3).

One registry describes every verifiable subject type: its model, the field that holds its id and the field that
holds its near-duplicate candidates (V10). Everything that hashes, names or walks a subject goes through here, so
the verifier, the ledger, the store, the renderer and the UI cannot disagree about identity.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel

from pm_agent.schemas.canonical import content_sha256
from pm_agent.schemas.claims import Claim
from pm_agent.schemas.drafts import KnowledgeArticleDraft, PrototypeSpec, RequirementDraft, TraceLink
from pm_agent.schemas.problems import DuplicateCandidate, InvestigationPack, ProductProblem

# Workflow bookkeeping that changes *after* verification; excluded so a report keeps matching its subject.
VOLATILE_FIELDS = frozenset({"verification_report_id", "approval_state"})


@dataclass(frozen=True)
class SubjectType:
    name: str
    model: type[BaseModel]
    id_field: str
    duplicates_field: str | None = None  # V10 candidates (with the PM's decisions) stored on the subject


SUBJECT_REGISTRY: dict[str, SubjectType] = {t.name: t for t in (
    SubjectType("requirement_draft", RequirementDraft, "requirement_id", duplicates_field="near_duplicates"),
    SubjectType("knowledge_article_draft", KnowledgeArticleDraft, "article_id"),
    SubjectType("prototype_spec", PrototypeSpec, "spec_id"),
    SubjectType("product_problem", ProductProblem, "problem_id", duplicates_field="duplicates"),
    SubjectType("trace_link", TraceLink, "link_id"),
    SubjectType("investigation_pack", InvestigationPack, "pack_id"),
)}
SUBJECT_TYPES: dict[str, type[BaseModel]] = {name: entry.model for name, entry in SUBJECT_REGISTRY.items()}


def subject_type_of(subject: BaseModel) -> SubjectType | None:
    """The registry entry for *subject*'s model (``None`` for models that are not verifiable subjects)."""
    return next((entry for entry in SUBJECT_REGISTRY.values() if isinstance(subject, entry.model)), None)


def subject_id(subject: BaseModel) -> str:
    entry = subject_type_of(subject)
    value = getattr(subject, entry.id_field, None) if entry else None
    return value if isinstance(value, str) else "unknown"


def subject_duplicates(subject: BaseModel) -> list[DuplicateCandidate]:
    """Near-duplicate candidates recorded on the subject (with the PM's link/merge/distinct decisions)."""
    entry = subject_type_of(subject)
    if entry is None or entry.duplicates_field is None:
        return []
    return list(getattr(subject, entry.duplicates_field, None) or [])


def subject_digest(subject: BaseModel) -> str:
    """The hash reports and approvals bind to: canonical JSON of the subject minus bookkeeping fields."""
    data = subject.model_dump(mode="json")
    for name in VOLATILE_FIELDS:
        data.pop(name, None)
    return content_sha256(data)


def iter_claims(obj: Any, path: str = "") -> Iterator[tuple[str, Claim]]:
    """Every ``Claim`` inside a (nested) subject, with its dotted path (e.g. ``fields.summary.claims[0]``)."""
    if isinstance(obj, Claim):
        yield path, obj
    elif isinstance(obj, BaseModel):
        for name in type(obj).model_fields:
            yield from iter_claims(getattr(obj, name), f"{path}.{name}" if path else name)
    elif isinstance(obj, dict):
        for key, value in obj.items():
            yield from iter_claims(value, f"{path}.{key}" if path else str(key))
    elif isinstance(obj, (list, tuple)):
        for index, value in enumerate(obj):
            yield from iter_claims(value, f"{path}[{index}]")
