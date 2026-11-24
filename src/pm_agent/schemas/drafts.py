"""Draft entities — everything that can become a proposed write (docs/10 §4–5)."""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from pm_agent.schemas.canonical import new_id
from pm_agent.schemas.claims import Claim, DraftField
from pm_agent.schemas.problems import DuplicateCandidate


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ApprovalState(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    CHANGES_REQUESTED = "changes_requested"


class FeasibilityVerdict(StrEnum):
    SUPPORTABLE = "supportable"
    CONSTRAINTS = "constraints"
    BLOCKED = "blocked"


class FeasibilityAssessment(_Model):
    """Advisory only (GOV3): Engineering decides."""

    verdict: FeasibilityVerdict
    claims: list[Claim] = Field(default_factory=list)
    affected_components: list[Claim] = Field(default_factory=list)
    advisory: Literal[True] = True
    engineering_signoff_required: Literal[True] = True


class RequirementDraft(_Model):
    requirement_id: str = Field(default_factory=lambda: new_id("draft-req"))
    version: int = 1
    problem_id: str
    template: str = "slb-ado-requirement-v1"
    work_item_type: str
    fields: dict[str, DraftField]
    feasibility: FeasibilityAssessment | None = None
    near_duplicates: list[DuplicateCandidate] = Field(default_factory=list)
    verification_report_id: str | None = None
    approval_state: ApprovalState = ApprovalState.PENDING

    @property
    def inferred_fields(self) -> list[str]:
        return [name for name, field in self.fields.items() if field.status.value == "inferred"]

    def all_claims(self) -> list[Claim]:
        claims = [c for f in self.fields.values() for c in f.claims]
        if self.feasibility:
            claims += self.feasibility.claims + self.feasibility.affected_components
        return claims


class KbAudience(StrEnum):
    INTERNAL = "internal"
    REGIONAL = "regional"
    GLOBAL = "global"


class KnowledgeArticleDraft(_Model):
    article_id: str = Field(default_factory=lambda: new_id("draft-kb"))
    version: int = 1
    problem_id: str
    title: Claim
    symptom: list[Claim]
    root_cause: list[Claim]
    resolution: list[Claim]
    workaround: list[Claim] = Field(default_factory=list)
    applies_to_product: str
    applies_to_versions: list[str] = Field(default_factory=list)
    audience: KbAudience = KbAudience.INTERNAL
    derived_from_resolution: str
    verification_report_id: str | None = None
    approval_state: ApprovalState = ApprovalState.PENDING

    def all_claims(self) -> list[Claim]:
        return [self.title, *self.symptom, *self.root_cause, *self.resolution, *self.workaround]


class SpecNode(_Model):
    """A node of a prototype screen: DLS component instance, text bound to a claim, or layout frame."""

    kind: Literal["instance", "text", "frame"]
    name: str
    component_key: str | None = None
    variant_props: dict[str, str] = Field(default_factory=dict)
    text_claim_id: str | None = None
    children: list["SpecNode"] = Field(default_factory=list)


class NewPattern(_Model):
    """FR-B3: every non-DLS element must be justified."""

    name: str
    justification: list[Claim]
    nearest_candidates: list[dict[str, str]] = Field(default_factory=list)


class PrototypeScreen(_Model):
    name: str
    width: int = Field(gt=0)
    height: int = Field(gt=0)
    nodes: list[SpecNode]


class PrototypeSpec(_Model):
    spec_id: str = Field(default_factory=lambda: new_id("spec"))
    brief_id: str
    requirement_id: str
    dls_library_file_key: str
    dls_published_version: str
    screens: list[PrototypeScreen]
    new_patterns: list[NewPattern] = Field(default_factory=list)

    def component_keys(self) -> list[str]:
        """Every DLS component key instantiated anywhere in the spec (V9 checks them against the DLS snapshot)."""
        keys: list[str] = []

        def walk(nodes: list[SpecNode]) -> None:
            for n in nodes:
                if n.kind == "instance" and n.component_key:
                    keys.append(n.component_key)
                walk(n.children)

        for screen in self.screens:
            walk(screen.nodes)
        return keys

    @property
    def reuse_ratio(self) -> float:
        reused = len(self.component_keys())
        total = reused + len(self.new_patterns)
        return 1.0 if total == 0 else reused / total


class TraceLinkType(StrEnum):
    CUSTOMER_REPORTED = "customer_reported"
    SIGNAL_OF = "signal_of"
    BUG_FOR = "bug_for"
    DISCUSSED_IN = "discussed_in"
    RESOLVED_BY = "resolved_by"
    CAPTURED_AS_KNOWLEDGE = "captured_as_knowledge"
    SPECIFIED_BY = "specified_by"
    PLANNED_IN = "planned_in"
    DECIDED_BY = "decided_by"


class TraceLink(_Model):
    link_id: str = Field(default_factory=lambda: new_id("tl"))
    from_type: str
    from_id: str
    to_type: str
    to_id: str
    link_type: TraceLinkType
    basis: list[Claim] = Field(default_factory=list)
    created_by: Literal["agent", "pm"] = "agent"
    status: Literal["proposed", "confirmed", "rejected"] = "proposed"
