"""HITL approval service — the logic behind ``mcp-ledger`` (docs/07 §3–§6).

``propose`` → ``decide`` (→ token minted on quorum) → ``consume_token`` → ``record_commit``.

Invariants enforced here:
* **H-1/H-4** a proposal needs a signed, fresh, commit-bound PASS report for *exactly* this action and payload;
* **H-2** decisions come from an authenticated human (IAP assertion), never from the proposing agent;
* **H-3** tokens are signed with a key only this service holds (KMS in production);
* **H-5** tokens are single-use, short-lived, revocable; consumption and ``COMMIT_STARTED`` are one atomic step.
The reference keeps state in memory under one lock; production keeps it in AlloyDB ``hitl.*`` tables and performs
the same checks inside one transaction per call.
"""

from __future__ import annotations

import itertools
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from pm_agent.crypto import Signer
from pm_agent.grounding.policy import PolicyEngine
from pm_agent.grounding.verifier import verify_report_signature
from pm_agent.hitl.catalog import ActionCatalog, ActionSpec, UnknownAction
from pm_agent.hitl.identity import IdentityError, IdentityVerifier, assertion_sha256
from pm_agent.hitl.ledger import Ledger
from pm_agent.hitl.tokens import mint_token
from pm_agent.placeholders import is_placeholder
from pm_agent.schemas import (
    ActionStatus,
    Actor,
    ApprovalDecision,
    ApprovalTokenClaims,
    Approver,
    Decision,
    LedgerEntryType,
    ProposedAction,
    SubjectRef,
    VerificationOutcome,
    VerificationReport,
    content_sha256,
    new_id,
    sha256_hex,
    utcnow,
)
from pm_agent.settings import Settings

LEDGER_ACTOR = Actor(type="system", id="mcp-ledger")
_AGENT_PREFIXES = ("spiffe://", "principal://agents.")


class ApprovalError(Exception):
    """A refused HITL operation. ``code`` is stable (tests, UI, metrics); messages never contain content."""

    def __init__(self, code: str, message: str = "") -> None:
        super().__init__(f"{code}: {message}" if message else code)
        self.code = code


@dataclass(frozen=True)
class IssuedToken:
    token: str
    claims: ApprovalTokenClaims


@dataclass(frozen=True)
class DecisionOutcome:
    action: ProposedAction
    token_issued: bool
    approvals: int
    required: int


@dataclass
class _TokenRecord:
    token: str
    claims: ApprovalTokenClaims
    consumed_by: str | None = None
    revoked: bool = False


class ApprovalService:  # pylint: disable=too-many-instance-attributes
    def __init__(self, *, settings: Settings, ledger: Ledger, catalog: ActionCatalog, token_signer: Signer,
                 verifier_keys: Mapping[str, bytes], identity: IdentityVerifier, policy: PolicyEngine | None = None,
                 clock: Callable[[], datetime] = utcnow) -> None:
        self._settings = settings
        self._ledger = ledger
        self._catalog = catalog
        self._signer = token_signer
        self._verifier_keys = dict(verifier_keys)
        self._identity = identity
        self._policy = policy or PolicyEngine(settings)
        self._clock = clock
        self._actions: dict[str, ProposedAction] = {}
        self._reports: dict[str, VerificationReport] = {}
        self._approvers: dict[str, list[Approver]] = {}
        self._decisions: list[ApprovalDecision] = []
        self._tokens: dict[str, _TokenRecord] = {}
        self._lock = threading.RLock()

    # ---------------------------------------------------------------------------------------------- queries
    def get_action(self, action_id: str) -> ProposedAction:
        with self._lock:
            return self._get(action_id).model_copy(deep=True)

    def get_report(self, report_id: str) -> VerificationReport:
        with self._lock:
            return self._reports[report_id]

    def pending(self) -> list[ProposedAction]:
        with self._lock:
            return [a.model_copy(deep=True) for a in self._actions.values()
                    if a.status is ActionStatus.PENDING_APPROVAL]

    def decisions(self, action_id: str) -> list[ApprovalDecision]:
        with self._lock:
            return [d for d in self._decisions if d.action_id == action_id]

    # ---------------------------------------------------------------------------------------------- propose
    def propose(self, *, action_type: str, target: dict[str, Any], payload: dict[str, Any], draft_ref: str,
                report: VerificationReport, proposed_by: str, session_id: str | None = None,
                trace_id: str | None = None) -> ProposedAction:
        """Register a write for PM approval — only with a signed, fresh, commit-bound PASS for this exact payload."""
        agent = Actor(type="agent", id=proposed_by)
        spec = self._admit(agent, action_type, target, payload, report)
        now = self._clock()
        action = ProposedAction(action_type=spec.action_type, target=target, payload=payload, draft_ref=draft_ref,
                                verification_report_id=report.report_id, proposed_by=proposed_by,
                                session_id=session_id, trace_id=trace_id, created_utc=now,
                                expires_utc=now + timedelta(hours=self._settings.hitl.proposal_ttl_hours))
        with self._lock:
            self._actions[action.action_id] = action
            self._reports[report.report_id] = report
            self._approvers[action.action_id] = []
            self._ledger.record(LedgerEntryType.ACTION_PROPOSED, actor=agent, subject_refs=[report.subject_ref],
                                payload={"action_id": action.action_id, "action_type": action_type,
                                         "payload_sha256": action.payload_sha256, "report_id": report.report_id,
                                         "draft_ref": draft_ref}, trace_id=trace_id)
            return action.model_copy(deep=True)

    def _admit(self, agent: Actor, action_type: str, target: dict[str, Any], payload: dict[str, Any],
               report: VerificationReport) -> ActionSpec:
        """Catalog (P-01) → report binding (H-1/H-4, P-08) → policy (P-02…P-07); every refusal is ledgered."""
        try:
            spec = self._catalog.get(action_type)
        except UnknownAction as exc:
            self._deny(agent, "unknown_action", action_type=action_type, subject=report.subject_ref)
            raise ApprovalError("unknown_action", str(exc)) from exc
        code = self._report_problem(spec, payload, report)
        if code:
            self._deny(agent, code, action_type=action_type, subject=report.subject_ref)
            raise ApprovalError(code)
        decision = self._policy.evaluate(action_type, payload, target)
        if not decision.allowed:
            self._deny(agent, "policy_denied", action_type=action_type, subject=report.subject_ref,
                       rules=decision.rule_ids)
            raise ApprovalError("policy_denied", ", ".join(decision.rule_ids))
        return spec

    def _report_problem(self, spec: ActionSpec, payload: dict[str, Any], report: VerificationReport) -> str | None:
        key = self._verifier_keys.get(report.signing_key_id or "")
        if key is None or not verify_report_signature(report, key):
            return "report_signature"
        if report.overall is not VerificationOutcome.PASS:
            return "report_not_pass"
        if not report.commit_bound:
            return "report_not_commit_bound"
        if report.target_action != spec.action_type.value or report.subject_ref.type not in spec.subject_types:
            return "report_action_mismatch"
        if report.target_payload_sha256 != content_sha256(payload):
            return "payload_not_verified"
        if self._clock() - report.created_utc > timedelta(hours=self._settings.verifier.report_max_age_hours):
            return "report_stale"  # P-08 approval freshness
        return None

    # ----------------------------------------------------------------------------------------------- decide
    def decide(self, *, action_id: str, decision: Decision, assertion: str, comment: str = "",
               expected_version: int | None = None) -> DecisionOutcome:
        """Record an authenticated PM decision; mint the approval token once the approver rule is satisfied."""
        approver = self._authenticate(assertion, action_id)
        with self._lock:
            action, spec = self._decidable(action_id, decision, expected_version)
            groups = self._authorize(spec, action, approver, decision)
            record = ApprovalDecision(action_id=action_id, decision=decision, approver=approver, comment=comment,
                                      decided_utc=self._clock(), authn_assertion_sha256=assertion_sha256(assertion))
            self._decisions.append(record)
            self._ledger.record(LedgerEntryType.DECISION_RECORDED, actor=Actor(type="human", id=approver.sub),
                                payload={"action_id": action_id, "decision": decision.value,
                                         "decision_id": record.decision_id, "groups": groups,
                                         "authn_assertion_sha256": record.authn_assertion_sha256,
                                         "comment_sha256": "sha256:" + sha256_hex(comment)},
                                trace_id=action.trace_id)
            return self._apply(action, spec, approver, decision)

    def _authenticate(self, assertion: str, action_id: str) -> Approver:
        try:
            return self._identity.verify(assertion)
        except IdentityError as exc:
            self._deny(LEDGER_ACTOR, "unauthenticated", action_id=action_id)
            raise ApprovalError("unauthenticated", "approver identity could not be verified") from exc

    def _decidable(self, action_id: str, decision: Decision,
                   expected_version: int | None) -> tuple[ProposedAction, ActionSpec]:
        action = self._get(action_id)
        if expected_version is not None and expected_version != action.version:
            raise ApprovalError("conflict", f"action is at version {action.version}")
        action = self._expire_if_due(action)
        allowed = {ActionStatus.PENDING_APPROVAL}
        if decision is Decision.REJECT:
            allowed.add(ActionStatus.APPROVED)  # reject after approve revokes the unused token
        if action.status not in allowed:
            raise ApprovalError("not_pending", f"action is {action.status.value}")
        return action, self._catalog.get(action.action_type.value)

    def _authorize(self, spec: ActionSpec, action: ProposedAction, approver: Approver,
                   decision: Decision) -> list[str]:
        human = Actor(type="human", id=approver.sub)
        if approver.sub == action.proposed_by or approver.sub.startswith(_AGENT_PREFIXES):
            self._deny(human, "separation_of_duties", action_id=action.action_id)
            raise ApprovalError("separation_of_duties", "agents cannot approve their own (or any) proposals")
        groups = self._matching_groups(spec, approver)
        if not groups:
            self._deny(human, "not_authorized", action_id=action.action_id)
            raise ApprovalError("not_authorized", "approver is not in an allowed group for this action")
        if decision is Decision.APPROVE and any(a.sub == approver.sub for a in self._approvers[action.action_id]):
            raise ApprovalError("duplicate_approver", "the same person cannot satisfy the two-person rule twice")
        return groups

    def _apply(self, action: ProposedAction, spec: ActionSpec, approver: Approver,
               decision: Decision) -> DecisionOutcome:
        approvers = self._approvers[action.action_id]
        issued = False
        if decision is Decision.APPROVE:
            approvers.append(approver)
            if self._quorum(spec, approvers):
                self._mint(action, spec, approver)
                action = self._update(action, ActionStatus.APPROVED)
                issued = True
            else:
                action = self._update(action, ActionStatus.PENDING_APPROVAL)
        else:
            self._revoke(action.action_id, Actor(type="human", id=approver.sub), reason=decision.value)
            status = ActionStatus.REJECTED if decision is Decision.REJECT else ActionStatus.CHANGES_REQUESTED
            action = self._update(action, status)
        return DecisionOutcome(action=action.model_copy(deep=True), token_issued=issued, approvals=len(approvers),
                               required=spec.min_approvals)

    def _resolve_group(self, logical: str) -> str:
        mapped = self._settings.hitl.idp_group_mapping.get(logical, logical)
        return logical if is_placeholder(mapped) else mapped  # local/dev only; strict envs reject placeholders

    def _matching_groups(self, spec: ActionSpec, approver: Approver) -> list[str]:
        held = set(approver.groups)
        return [group for group in spec.approver_groups if self._resolve_group(group) in held]

    def _quorum(self, spec: ActionSpec, approvers: list[Approver]) -> bool:
        if len(approvers) < spec.min_approvals:
            return False
        if not spec.require_each_group:
            return True
        groups = spec.approver_groups
        memberships = [set(self._matching_groups(spec, a)) for a in approvers]
        return any(all(groups[i] in memberships[person] for i, person in enumerate(assignment))
                   for assignment in itertools.permutations(range(len(approvers)), len(groups)))

    def _mint(self, action: ProposedAction, spec: ActionSpec, approver: Approver) -> None:
        now = int(self._clock().timestamp())
        report = self._reports[action.verification_report_id]
        claims = ApprovalTokenClaims(iss=self._settings.hitl.token_issuer, sub=approver.sub,
                                     aud=self._settings.hitl.token_audience, jti=new_id("tok"), iat=now,
                                     exp=now + 60 * self._settings.hitl.token_ttl_minutes, act=action.action_id,
                                     pah=str(action.payload_sha256), tgt=action.target, scp=[spec.scope],
                                     vrf=report.subject_sha256)
        self._tokens[action.action_id] = _TokenRecord(token=mint_token(claims, self._signer), claims=claims)
        self._ledger.record(LedgerEntryType.TOKEN_ISSUED, actor=LEDGER_ACTOR, payload={
            "action_id": action.action_id, "jti": claims.jti, "exp": claims.exp, "scope": spec.scope,
            "kid": self._signer.key_id}, trace_id=action.trace_id)

    def _revoke(self, action_id: str, actor: Actor, reason: str) -> None:
        record = self._tokens.get(action_id)
        if record is None or record.revoked or record.consumed_by:
            return
        record.revoked = True
        self._ledger.record(LedgerEntryType.TOKEN_REVOKED, actor=actor, payload={
            "action_id": action_id, "jti": record.claims.jti, "reason": reason})

    # -------------------------------------------------------------------------------------------- consume
    def consume_token(self, *, action_id: str, consumer: str) -> IssuedToken:
        """Atomically burn the token and record ``TOKEN_CONSUMED`` + ``COMMIT_STARTED`` (single use, H-5)."""
        executor = Actor(type="agent", id=consumer)
        with self._lock:
            action = self._get(action_id)
            record = self._tokens.get(action_id)
            code = None
            if record is not None and record.consumed_by:
                code = "token_consumed"
            elif record is not None and record.revoked:
                code = "token_revoked"
            elif record is None or action.status is not ActionStatus.APPROVED:
                code = "not_approved"
            elif self._clock().timestamp() >= record.claims.exp:
                code = "token_expired"
                self._update(action, ActionStatus.EXPIRED)
            if code or record is None:
                self._deny(executor, code or "not_approved", action_id=action_id)
                raise ApprovalError(code or "not_approved")
            record.consumed_by = consumer
            self._update(action, ActionStatus.COMMITTING)
            self._ledger.record(LedgerEntryType.TOKEN_CONSUMED, actor=executor,
                                payload={"action_id": action_id, "jti": record.claims.jti}, trace_id=action.trace_id)
            self._ledger.record(LedgerEntryType.COMMIT_STARTED, actor=executor,
                                payload={"action_id": action_id}, trace_id=action.trace_id)
            return IssuedToken(token=record.token, claims=record.claims)

    def record_commit(self, *, action_id: str, actor_id: str, succeeded: bool,
                      external_ref: dict[str, str] | None = None, error: str | None = None,
                      changes_requested: bool = False, report_id: str | None = None) -> ProposedAction:
        """Close a started commit: ``COMMIT_SUCCEEDED`` (COMMITTED) or ``COMMIT_FAILED`` (FAILED/CHANGES_REQUESTED)."""
        executor = Actor(type="agent", id=actor_id)
        with self._lock:
            action = self._get(action_id)
            if action.status is not ActionStatus.COMMITTING:
                raise ApprovalError("not_committing", f"action is {action.status.value}")
            details: dict[str, Any] = {"action_id": action_id, "reverification_report_id": report_id}
            if succeeded:
                details |= dict(external_ref or {})
                entry_type, status = LedgerEntryType.COMMIT_SUCCEEDED, ActionStatus.COMMITTED
            else:
                details["error"] = (error or "unknown")[:256]
                entry_type = LedgerEntryType.COMMIT_FAILED
                status = ActionStatus.CHANGES_REQUESTED if changes_requested else ActionStatus.FAILED
            self._ledger.record(entry_type, actor=executor, payload=details, trace_id=action.trace_id)
            return self._update(action, status).model_copy(deep=True)

    # ---------------------------------------------------------------------------------------------- expiry
    def expire_stale(self) -> list[str]:
        """Expire proposals past their TTL and approvals whose token lapsed unused (P-08)."""
        with self._lock:
            before = {a.action_id: a.status for a in self._actions.values()}
            for action in list(self._actions.values()):
                self._expire_if_due(action)
            return [aid for aid, status in before.items()
                    if status is not ActionStatus.EXPIRED and self._actions[aid].status is ActionStatus.EXPIRED]

    def _expire_if_due(self, action: ProposedAction) -> ProposedAction:
        now = self._clock()
        token = self._tokens.get(action.action_id)
        due = (action.status is ActionStatus.PENDING_APPROVAL and action.expires_utc is not None
               and now >= action.expires_utc) or (action.status is ActionStatus.APPROVED and token is not None
                                                  and now.timestamp() >= token.claims.exp)
        if not due:
            return action
        self._ledger.record(LedgerEntryType.ACTION_EXPIRED, actor=LEDGER_ACTOR,
                            payload={"action_id": action.action_id, "from_status": action.status.value})
        return self._update(action, ActionStatus.EXPIRED)

    # ------------------------------------------------------------------------------------------- internals
    def _get(self, action_id: str) -> ProposedAction:
        try:
            return self._actions[action_id]
        except KeyError:
            raise ApprovalError("unknown_action_id", action_id) from None

    def _update(self, action: ProposedAction, status: ActionStatus) -> ProposedAction:
        updated = action.model_copy(update={"status": status, "version": action.version + 1})
        self._actions[action.action_id] = updated
        return updated

    def _deny(self, actor: Actor, code: str, *, action_type: str | None = None, action_id: str | None = None,
              subject: SubjectRef | None = None, rules: list[str] | None = None) -> None:
        payload: dict[str, Any] = {"code": code}
        if action_type:
            payload["action_type"] = action_type
        if action_id:
            payload["action_id"] = action_id
        if rules:
            payload["rules"] = rules
        self._ledger.record(LedgerEntryType.POLICY_DENIED, actor=actor, subject_refs=[subject] if subject else [],
                            payload=payload)
