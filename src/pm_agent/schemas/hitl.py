"""HITL entities: proposed actions, decisions, approval tokens, ledger entries, reports (docs/07, docs/10 §6)."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from pm_agent.schemas.canonical import content_sha256, new_id, utcnow


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ActionType(StrEnum):
    """Closed, deny-by-default catalog of state-changing actions (docs/07 §2)."""

    ADO_CREATE_WORK_ITEM = "ADO_CREATE_WORK_ITEM"
    ADO_ADD_LINKS = "ADO_ADD_LINKS"
    KB_PUBLISH_ARTICLE = "KB_PUBLISH_ARTICLE"
    FIGMA_RENDER_PROTOTYPE = "FIGMA_RENDER_PROTOTYPE"
    FIGMA_POST_COMMENT = "FIGMA_POST_COMMENT"
    PROTOTYPE_RELEASE_INTERNAL = "PROTOTYPE_RELEASE_INTERNAL"
    OUTREACH_SEND_INTERNAL = "OUTREACH_SEND_INTERNAL"
    ROADMAP_TRACE_LINK = "ROADMAP_TRACE_LINK"


class ActionStatus(StrEnum):
    PENDING_APPROVAL = "PENDING_APPROVAL"
    APPROVED = "APPROVED"
    CHANGES_REQUESTED = "CHANGES_REQUESTED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"
    COMMITTING = "COMMITTING"
    COMMITTED = "COMMITTED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class ProposedAction(_Model):
    action_id: str = Field(default_factory=lambda: new_id("act"))
    action_type: ActionType
    target: dict[str, Any]
    payload: dict[str, Any]
    payload_sha256: str | None = None
    draft_ref: str
    verification_report_id: str
    risk_flags: list[str] = Field(default_factory=list)
    proposed_by: str
    session_id: str | None = None
    trace_id: str | None = None
    created_utc: datetime = Field(default_factory=utcnow)
    expires_utc: datetime | None = None
    status: ActionStatus = ActionStatus.PENDING_APPROVAL
    version: int = 1

    @model_validator(mode="after")
    def _bind_payload(self) -> "ProposedAction":
        digest = content_sha256(self.payload)
        if self.payload_sha256 is None:
            object.__setattr__(self, "payload_sha256", digest)
        elif self.payload_sha256 != digest:
            raise ValueError("payload_sha256 does not match payload")
        return self


class Decision(StrEnum):
    APPROVE = "approve"
    REJECT = "reject"
    REQUEST_CHANGES = "request_changes"


class Approver(_Model):
    sub: str
    email: str
    idp: str
    groups: list[str] = Field(default_factory=list)


class ApprovalDecision(_Model):
    decision_id: str = Field(default_factory=lambda: new_id("dec"))
    action_id: str
    decision: Decision
    approver: Approver
    comment: str = ""
    decided_utc: datetime = Field(default_factory=utcnow)
    authn_assertion_sha256: str


class ApprovalTokenClaims(_Model):
    """JWS payload of an approval token (docs/07 §6)."""

    iss: str
    sub: str
    aud: str
    jti: str
    iat: int
    exp: int
    act: str
    pah: str
    tgt: dict[str, Any]
    scp: list[str] = Field(min_length=1)
    vrf: str

    @model_validator(mode="after")
    def _expiry_after_issue(self) -> "ApprovalTokenClaims":
        if self.exp <= self.iat:
            raise ValueError("exp must be after iat")
        return self


class LedgerEntryType(StrEnum):
    DRAFT_SAVED = "DRAFT_SAVED"
    VERIFICATION_COMPLETED = "VERIFICATION_COMPLETED"
    ACTION_PROPOSED = "ACTION_PROPOSED"
    ACTION_EXPIRED = "ACTION_EXPIRED"
    DECISION_RECORDED = "DECISION_RECORDED"
    TOKEN_ISSUED = "TOKEN_ISSUED"
    TOKEN_CONSUMED = "TOKEN_CONSUMED"
    TOKEN_REVOKED = "TOKEN_REVOKED"
    COMMIT_STARTED = "COMMIT_STARTED"
    COMMIT_SUCCEEDED = "COMMIT_SUCCEEDED"
    COMMIT_FAILED = "COMMIT_FAILED"
    POLICY_DENIED = "POLICY_DENIED"
    TOOL_CALL_DENIED = "TOOL_CALL_DENIED"
    PM_INPUT_RECORDED = "PM_INPUT_RECORDED"
    PROBLEM_MERGED = "PROBLEM_MERGED"
    CONFIG_CHANGED = "CONFIG_CHANGED"
    ANCHOR_WRITTEN = "ANCHOR_WRITTEN"


class Actor(_Model):
    type: Literal["agent", "human", "system"]
    id: str


class SubjectRef(_Model):
    type: str
    id: str
    version: int | None = None
    sha256: str | None = None


class LedgerEntry(_Model):
    seq: int = Field(ge=1)
    entry_id: str
    entry_type: LedgerEntryType
    subject_refs: list[SubjectRef] = Field(default_factory=list)
    actor: Actor
    payload: dict[str, Any] = Field(default_factory=dict)
    prev_hash: str
    entry_hash: str
    recorded_utc: datetime
    trace_id: str | None = None


class CheckStatus(StrEnum):
    PASS = "PASS"
    FAIL = "FAIL"
    SKIPPED = "SKIPPED"
    NEEDS_PM_DECISION = "NEEDS_PM_DECISION"


class CheckResult(_Model):
    check_id: str
    status: CheckStatus
    details: str = ""
    claim_ids: list[str] = Field(default_factory=list)


class VerificationOutcome(StrEnum):
    PASS = "PASS"
    FAIL = "FAIL"
    NEEDS_PM_DECISION = "NEEDS_PM_DECISION"


class VerificationReport(_Model):
    """Signed result of ``verify_subject``. Commit-bound reports also bind the exact target payload hash."""

    report_id: str = Field(default_factory=lambda: new_id("vr"))
    subject_ref: SubjectRef
    subject_sha256: str
    verifier_version: str
    policy_versions: dict[str, str] = Field(default_factory=dict)
    checks: list[CheckResult]
    overall: VerificationOutcome
    blocked_claim_ids: list[str] = Field(default_factory=list)
    commit_bound: bool = False
    target_action: str | None = None
    target_payload_sha256: str | None = None
    signing_key_id: str | None = None
    created_utc: datetime = Field(default_factory=utcnow)
    signature: str | None = None

    def check(self, check_id: str) -> CheckResult | None:
        """The result for one check id (``V0``…``V10``), if present."""
        return next((c for c in self.checks if c.check_id == check_id), None)
