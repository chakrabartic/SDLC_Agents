"""LLM-facing output schemas (``output_schema`` of every single-turn LLM node).

Deliberately separate from the internal entities: models emit ``evidence_id`` + verbatim quote only; code converts
to ``ClaimDraft`` and the Verifier resolves spans. Invalid model output is never repaired — a claim whose citation
cannot be parsed loses that citation and is then blocked by V1 like any other unsupported claim.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, ValidationError

from pm_agent.schemas import CitedQuote, ClaimDraft, ClaimKind

LlmClaimKind = Literal["fact", "classification", "pm_judgment", "inference"]
ChecklistItem = Literal["who_affected", "current_vs_desired_workflow", "should_do_should_not",
                        "acceptance_criteria", "edge_cases", "dependencies_constraints"]
Capability = Literal["ask", "review_problems", "build_problem", "define_requirement", "design_prototype",
                     "investigate_issue", "capture_knowledge", "trace_roadmap", "surface_trends", "unsupported"]


class LlmCitation(BaseModel):
    evidence_id: str = Field(description="An evidence_id exactly as given in the evidence pack.")
    quote: str = Field(description="Verbatim excerpt copied character-for-character from that evidence text.")


class LlmClaim(BaseModel):
    text: str = Field(description="One atomic statement.")
    kind: LlmClaimKind
    citations: list[LlmCitation] = Field(default_factory=list)


class Intent(BaseModel):
    capability: Capability
    problem_id: str | None = None
    signal_id: str | None = None
    question: str | None = None


class ReadinessItemOut(BaseModel):
    item: ChecklistItem
    status: Literal["present", "partial", "missing"]
    claims: list[LlmClaim] = Field(default_factory=list)
    gap: str | None = Field(default=None, description="What is missing, phrased without new facts.")


class ReadinessOut(BaseModel):
    items: list[ReadinessItemOut]


class QuestionOut(BaseModel):
    checklist_item: ChecklistItem
    text: str = Field(description="A question to the PM; do not assert facts that are not in the evidence.")
    why: list[LlmClaim] = Field(default_factory=list)


class QuestionsOut(BaseModel):
    questions: list[QuestionOut]


class FieldOut(BaseModel):
    name: str = Field(description="One of the template field names given in the task.")
    claims: list[LlmClaim] = Field(default_factory=list)


class RequirementOut(BaseModel):
    fields: list[FieldOut]


class FeasibilityOut(BaseModel):
    verdict: Literal["supportable", "constraints", "blocked"]
    claims: list[LlmClaim] = Field(default_factory=list)
    affected_components: list[LlmClaim] = Field(default_factory=list)


class ProblemOut(BaseModel):
    title: LlmClaim
    classification: LlmClaim
    problem_class: Literal["feature_request", "workflow_problem", "knowledge_gap", "defect", "enhancement"]
    confidence: Literal["high", "medium", "low"]
    impact_business: list[LlmClaim] = Field(default_factory=list)
    impact_customer: list[LlmClaim] = Field(default_factory=list)


class AnswerOut(BaseModel):
    claims: list[LlmClaim]


class KbArticleOut(BaseModel):
    title: LlmClaim
    symptom: list[LlmClaim]
    root_cause: list[LlmClaim]
    resolution: list[LlmClaim]
    workaround: list[LlmClaim] = Field(default_factory=list)


class SpecNodeOut(BaseModel):
    """Flat node list (parent by index) — recursive schemas are avoided for structured output."""

    kind: Literal["instance", "text", "frame"]
    name: str
    parent: int | None = Field(default=None, description="Index of the parent node in this list, or null.")
    component_key: str | None = Field(default=None, description="A DLS component key from the catalog given.")
    variant_props: dict[str, str] = Field(default_factory=dict)
    text_claim: int | None = Field(default=None, description="Index into requirement_claims for text nodes.")


class ScreenOut(BaseModel):
    name: str
    width: int = 1440
    height: int = 900
    nodes: list[SpecNodeOut]


class NewPatternOut(BaseModel):
    name: str
    justification: list[LlmClaim]
    nearest_candidates: list[str] = Field(default_factory=list)


class PrototypeOut(BaseModel):
    screens: list[ScreenOut]
    new_patterns: list[NewPatternOut] = Field(default_factory=list)


def to_claim_draft(claim: LlmClaim | dict[str, object]) -> ClaimDraft:
    """Convert model output to a ``ClaimDraft``; unparseable citations are dropped (⇒ V1 blocks if required)."""
    parsed = claim if isinstance(claim, LlmClaim) else LlmClaim.model_validate(claim)
    cites: list[CitedQuote] = []
    for citation in parsed.citations:
        try:
            cites.append(CitedQuote(evidence_id=citation.evidence_id, quote=citation.quote))
        except ValidationError:
            continue
    return ClaimDraft(text=parsed.text.strip() or "(empty)", kind=ClaimKind(parsed.kind), citations=cites)


def to_claim_drafts(claims: list[LlmClaim] | list[dict[str, object]]) -> list[ClaimDraft]:
    return [to_claim_draft(c) for c in claims]
