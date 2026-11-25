"""Deterministic view models: verified entities → JSON-safe data-model content (internal).

Claims are copied **verbatim** (``Claim.model_dump(mode="json")`` — docs/10 field names) so ClaimList renders the
exact verified statements with their citations; only grouping and labels are added here.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from pydantic import BaseModel

from pm_agent.a2ui._ui import label
from pm_agent.schemas import (
    Citation,
    Claim,
    ClaimKind,
    DuplicateCandidate,
    FieldStatus,
    InvestigationPack,
    KnowledgeArticleDraft,
    ProductProblem,
    PrototypeSpec,
    RequirementDraft,
    Signal,
    SpecNode,
    TraceLink,
)
from pm_agent.schemas.subjects import iter_claims

Section = dict[str, Any]
Row = dict[str, str | int | float | bool | None]

DUPLICATE_COLUMNS = (("ref", "Reference"), ("system", "System"), ("similarity", "Similarity"), ("method", "Method"),
                     ("decision", "Decision"))


def claim_status(claims: Sequence[Claim]) -> FieldStatus:
    """Status for a plain claim list: empty ⇒ missing, inference only ⇒ inferred, otherwise populated."""
    if not claims:
        return FieldStatus.MISSING
    if all(c.kind is ClaimKind.INFERENCE for c in claims):
        return FieldStatus.INFERRED
    return FieldStatus.POPULATED


def section(key: str, title: str, claims: Sequence[Claim], status: FieldStatus | None = None) -> Section:
    return {"key": key, "label": title, "status": (status or claim_status(claims)).value,
            "claims": [c.model_dump(mode="json") for c in claims]}


def requirement_sections(draft: RequirementDraft, template_fields: Sequence[str] | None = None) -> list[Section]:
    """Template fields in order (absent ones *missing*), then every other drafted field — nothing is hidden."""
    keys = list(dict.fromkeys(template_fields)) if template_fields else list(draft.fields)
    keys += [k for k in draft.fields if k not in keys]
    sections = []
    for key in keys:
        field = draft.fields.get(key)
        sections.append(section(key, label(key), field.claims if field else [],
                                field.status if field else FieldStatus.MISSING))
    if draft.feasibility is not None:
        feasibility = draft.feasibility
        sections.append(section("feasibility",
                                f"Feasibility: {feasibility.verdict.value} (advisory — Engineering decides)",
                                [*feasibility.claims, *feasibility.affected_components]))
    return sections


def knowledge_sections(article: KnowledgeArticleDraft) -> list[Section]:
    return [section("title", "Title", [article.title]), section("symptom", "Symptom", article.symptom),
            section("root_cause", "Root cause", article.root_cause),
            section("resolution", "Resolution", article.resolution),
            section("workaround", "Workaround", article.workaround)]


def problem_claim_groups(problem: ProductProblem) -> list[tuple[str, str, list[Claim]]]:
    """``(key, label, claims)`` for the problem's drafted labels, aggregates and impacts (absent ones skipped)."""
    groups: list[tuple[str, str, list[Claim]]] = [
        ("title", "Title", [problem.title]), ("classification", "Classification", [problem.classification])]
    for key, title, value in (("affected_customers", "Affected customers", problem.affected_customers),
                              ("affected_regions", "Affected regions", problem.affected_regions),
                              ("request_frequency", "Request frequency", problem.request_frequency)):
        if value is not None:
            groups.append((key, title, [value]))
    for key, title, values in (("impact_business", "Business impact", problem.impact_business),
                               ("impact_customer", "Customer impact", problem.impact_customer)):
        if values:
            groups.append((key, title, list(values)))
    return groups


def problem_sections(problem: ProductProblem) -> list[Section]:
    return [section(key, title, claims) for key, title, claims in problem_claim_groups(problem)]


def pack_sections(pack: InvestigationPack, *, include_timeline: bool) -> list[Section]:
    """Non-empty claim groups of an investigation pack; the timeline only when no Timeline component shows it."""
    groups: list[tuple[str, str, list[Claim]]] = [
        ("prior_resolutions", "Prior resolutions", list(pack.prior_resolutions)),
        ("suggested_engagements", "Suggested engagements", list(pack.suggested_engagements))]
    if include_timeline:
        groups.insert(0, ("timeline", "Timeline", list(pack.timeline)))
    if pack.outcome is not None:
        groups.append(("outcome", "Proposed outcome (PM confirms)", [pack.outcome]))
    return [section(key, title, claims) for key, title, claims in groups if claims]


def prototype_sections(spec: PrototypeSpec) -> list[Section]:
    return [section(f"pattern_{i}", f"New pattern: {p.name}", p.justification) for i, p in enumerate(spec.new_patterns)]


def subject_sections(subject: BaseModel) -> list[Section]:
    """Every claim of a verified subject, grouped for display; leftovers land in 'Other verified statements'."""
    if isinstance(subject, RequirementDraft):
        sections = requirement_sections(subject)
    elif isinstance(subject, KnowledgeArticleDraft):
        sections = knowledge_sections(subject)
    elif isinstance(subject, ProductProblem):
        sections = problem_sections(subject)
    elif isinstance(subject, InvestigationPack):
        sections = pack_sections(subject, include_timeline=True)
    elif isinstance(subject, PrototypeSpec):
        sections = prototype_sections(subject)
    elif isinstance(subject, TraceLink):
        sections = [section("basis", f"Basis: {subject.from_type} {subject.from_id} → {subject.link_type.value} → "
                                     f"{subject.to_type} {subject.to_id}", subject.basis)]
    else:
        sections = []
    shown = {json.dumps(c, sort_keys=True) for s in sections for c in s["claims"]}
    leftovers: dict[str, list[Claim]] = {}
    for path, claim in iter_claims(subject):
        if json.dumps(claim.model_dump(mode="json"), sort_keys=True) not in shown:
            leftovers.setdefault(re.split(r"[.\[]", path, maxsplit=1)[0], []).append(claim)
    sections += [section(f"other_{key}", label(key), claims) for key, claims in leftovers.items()]
    return sections


def table(columns: Sequence[tuple[str, str]], rows: list[Row]) -> dict[str, Any]:
    return {"columns": [{"key": key, "label": title} for key, title in columns], "rows": rows}


def scalar(value: Any) -> str | int | float | bool | None:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return json.dumps(value, sort_keys=True, ensure_ascii=False, default=str)


def flatten(value: Any, prefix: str = "") -> list[Row]:
    """``{"fields": {"System.Title": "x"}}`` → ``[{"field": "fields/System.Title", "value": "x"}]`` (sorted keys)."""
    if isinstance(value, Mapping) and value:
        rows: list[Row] = []
        for key in sorted(value, key=str):
            rows += flatten(value[key], f"{prefix}/{key}" if prefix else str(key))
        return rows
    if isinstance(value, list) and any(isinstance(item, (Mapping, list)) for item in value):
        return [row for i, item in enumerate(value) for row in flatten(item, f"{prefix}/{i}" if prefix else str(i))]
    return [{"field": prefix or "(payload)", "value": scalar(value)}]


def duplicate_row(candidate: DuplicateCandidate) -> Row:
    return {"ref": candidate.ref, "system": candidate.system, "similarity": round(candidate.similarity, 4),
            "method": candidate.method, "decision": candidate.decision.value}


def signal_ref(citation: Citation, signal: Signal | None) -> dict[str, Any]:
    """Provenance shown next to evidence (pseudonymous author only — re-identification is the BFF's job)."""
    if signal is not None:
        dumped = signal.model_dump(mode="json")
        return {key: dumped.get(key) for key in ("signal_id", "source_system", "source_record_id", "permalink",
                                                 "record_type", "captured_utc", "title", "author")}
    return {"signal_id": citation.signal_id, "source_system": citation.source_system.value,
            "source_record_id": citation.source_record_id, "permalink": citation.permalink, "record_type": None,
            "captured_utc": None, "title": None, "author": None}


def evidence_items(claims: Iterable[Claim], signals: Mapping[str, Signal]) -> list[dict[str, Any]]:
    """Unique cited spans (evidence id + offsets), in order of first citation."""
    items: list[dict[str, Any]] = []
    seen: set[tuple[str, int, int]] = set()
    for claim in claims:
        for citation in claim.citations:
            key = (citation.evidence_id, citation.span.start, citation.span.end)
            if key not in seen:
                seen.add(key)
                items.append({"citation": citation.model_dump(mode="json"),
                              "signal": signal_ref(citation, signals.get(citation.signal_id))})
    return items


def mapping_rows(spec: PrototypeSpec) -> list[Row]:
    """One row per spec node: where it sits, which DLS component it instantiates, which claim its copy comes from."""
    rows: list[Row] = []

    def walk(screen: str, nodes: Sequence[SpecNode], parents: tuple[str, ...]) -> None:
        for node in nodes:
            path = (*parents, node.name)
            rows.append({"screen": screen, "path": "/".join(path), "kind": node.kind,
                         "component_key": node.component_key or "",
                         "variant_props": json.dumps(node.variant_props, sort_keys=True) if node.variant_props else "",
                         "text_claim_id": node.text_claim_id or ""})
            walk(screen, node.children, path)

    for screen in spec.screens:
        walk(screen.name, screen.nodes, ())
    return rows
