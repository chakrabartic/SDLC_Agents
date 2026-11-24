"""Problem & analysis entities (docs/10 §3)."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from pm_agent.schemas.canonical import new_id
from pm_agent.schemas.claims import Claim
from pm_agent.schemas.evidence import PersonRef


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ProblemClass(StrEnum):
    FEATURE_REQUEST = "feature_request"
    WORKFLOW_PROBLEM = "workflow_problem"
    KNOWLEDGE_GAP = "knowledge_gap"
    DEFECT = "defect"
    ENHANCEMENT = "enhancement"


class Confidence(StrEnum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"  # low ⇒ PM confirmation required before downstream use (requirements §13)


class LinkType(StrEnum):
    HARD_REF = "hard_ref"          # deterministic cross-reference (e.g. ADO URL in a ServiceNow note)
    SEMANTIC = "semantic"          # similarity above the auto-link threshold
    ADJUDICATED = "adjudicated"    # grey zone resolved by the cited link adjudicator
    PM_CONFIRMED = "pm_confirmed"


class SignalLink(_Model):
    signal_id: str
    link_type: LinkType
    score: float = Field(ge=0.0, le=1.0)
    linked_by: str


class DuplicateDecision(StrEnum):
    PENDING = "pending"
    LINKED = "linked"
    MERGED = "merged"
    DISTINCT = "distinct"


class DuplicateCandidate(_Model):
    ref: str
    system: str
    similarity: float = Field(ge=0.0, le=1.0)
    method: str
    decision: DuplicateDecision = DuplicateDecision.PENDING


class ReadinessItemName(StrEnum):
    WHO_AFFECTED = "who_affected"
    CURRENT_VS_DESIRED_WORKFLOW = "current_vs_desired_workflow"
    SHOULD_DO_SHOULD_NOT = "should_do_should_not"
    ACCEPTANCE_CRITERIA = "acceptance_criteria"
    EDGE_CASES = "edge_cases"
    DEPENDENCIES_CONSTRAINTS = "dependencies_constraints"


class ReadinessStatus(StrEnum):
    PRESENT = "present"
    PARTIAL = "partial"
    MISSING = "missing"


class ReadinessItem(_Model):
    item: ReadinessItemName
    status: ReadinessStatus
    claims: list[Claim] = Field(default_factory=list)
    gap: str | None = None
    question_ids: list[str] = Field(default_factory=list)


class ReadinessAssessment(_Model):
    checklist_id: str = "slb-readiness-v1"
    items: list[ReadinessItem]

    @property
    def gaps(self) -> list[ReadinessItem]:
        return [i for i in self.items if i.status is not ReadinessStatus.PRESENT]


class QuestionStatus(StrEnum):
    OPEN = "open"
    ANSWERED = "answered"
    DISMISSED = "dismissed"


class SuggestedRespondent(_Model):
    person: PersonRef
    basis: Claim


class ClarifyingQuestion(_Model):
    question_id: str = Field(default_factory=lambda: new_id("q"))
    checklist_item: ReadinessItemName
    text: str = Field(min_length=1)
    why_asked: list[Claim] = Field(default_factory=list)
    suggested_respondents: list[SuggestedRespondent] = Field(default_factory=list)
    status: QuestionStatus = QuestionStatus.OPEN
    answer_signal_id: str | None = None


class ProblemStatus(StrEnum):
    NEW = "new"
    GATHERING = "gathering"
    READY_FOR_DRAFT = "ready_for_draft"
    DRAFTED = "drafted"
    PENDING_APPROVAL = "pending_approval"
    APPROVED = "approved"
    COMMITTED = "committed"
    CLOSED = "closed"


class ProductProblem(_Model):
    problem_id: str = Field(default_factory=lambda: new_id("prb"))
    version: int = 1
    title: Claim
    product_line: str | None = None
    classification: Claim
    classification_confidence: Confidence = Confidence.LOW
    linked_signals: list[SignalLink] = Field(default_factory=list)
    affected_customers: Claim | None = None
    affected_regions: Claim | None = None
    request_frequency: Claim | None = None
    impact_business: list[Claim] = Field(default_factory=list)
    impact_customer: list[Claim] = Field(default_factory=list)
    duplicates: list[DuplicateCandidate] = Field(default_factory=list)
    readiness: ReadinessAssessment | None = None
    open_questions: list[ClarifyingQuestion] = Field(default_factory=list)
    sources_unavailable: list[str] = Field(default_factory=list)
    owner_pm: PersonRef | None = None
    status: ProblemStatus = ProblemStatus.NEW


class Outcome(StrEnum):
    WORKAROUND = "workaround"
    WORKFLOW_CLARIFICATION = "workflow_clarification"
    DEFECT = "defect"
    NON_DEFECT = "non_defect"


class InvestigationPack(_Model):
    pack_id: str = Field(default_factory=lambda: new_id("inv"))
    trigger_signal_id: str
    related: list[SignalLink] = Field(default_factory=list)
    prior_resolutions: list[Claim] = Field(default_factory=list)
    timeline: list[Claim] = Field(default_factory=list)
    suggested_engagements: list[Claim] = Field(default_factory=list)
    outcome: Claim | None = None
    outcome_label: Outcome | None = None
