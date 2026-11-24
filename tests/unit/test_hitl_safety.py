"""HITL safety suite — CI gate 6 / AC9 (docs/07 §1, §9; docs/13).

No write may happen without a fresh, PM-issued, single-use approval token bound to the exact verified payload.
Every refusal below is paired with the positive control that proves the happy path still works.
"""
from __future__ import annotations

from datetime import timedelta

import pytest

from pm_agent.crypto import LocalEcdsaSigner
from pm_agent.gateway import ToolCallError
from pm_agent.hitl.approvals import ApprovalError
from pm_agent.hitl.tokens import TokenError, mint_token
from pm_agent.schemas import (
    ActionStatus,
    ActionType,
    ApprovalTokenClaims,
    Decision,
    LedgerEntryType,
    SignalStatus,
    VerificationOutcome,
    content_sha256,
)
from tests.support import (
    AGENT_ID,
    EXECUTOR_ID,
    OUTSIDER,
    PM,
    PM2,
    UX,
    ado_payload,
    grounded_draft,
    make_hitl,
    prototype_spec,
)

ADO = ActionType.ADO_CREATE_WORK_ITEM.value
FIGMA = ActionType.FIGMA_RENDER_PROTOTYPE.value


@pytest.fixture(name="hitl")
def _hitl():  # type: ignore[no-untyped-def]
    return make_hitl()


def _approve(hitl, action, *people):  # type: ignore[no-untyped-def]
    outcome = None
    for person in people:
        outcome = hitl.approvals.decide(action_id=action.action_id, decision=Decision.APPROVE,
                                        assertion=hitl.identities.issue(person))
    return outcome


def _code(excinfo) -> str:  # type: ignore[no-untyped-def]
    return str(excinfo.value.code)


# ----------------------------------------------------------------------------------------- positive control
def test_token_positive_control_commits_exactly_once_with_complete_ledger(hitl) -> None:  # type: ignore[no-untyped-def]
    action = hitl.propose_ado()
    outcome = _approve(hitl, action, PM)
    assert outcome.token_issued and outcome.action.status is ActionStatus.APPROVED
    result = hitl.executor.execute(action.action_id, executor_id=EXECUTOR_ID)
    assert result.outcome == "committed", result
    assert result.external_ref and result.external_ref["external_id"]
    assert len(hitl.backend.preflights()) == 1 and len(hitl.backend.creates()) == 1
    assert hitl.backend.creates()[0]["payload"] == action.payload
    entry_types = [e.entry_type for e in hitl.ledger.query(action_id=action.action_id)]
    assert entry_types == [LedgerEntryType.ACTION_PROPOSED, LedgerEntryType.DECISION_RECORDED,
                           LedgerEntryType.TOKEN_ISSUED, LedgerEntryType.TOKEN_CONSUMED,
                           LedgerEntryType.COMMIT_STARTED, LedgerEntryType.COMMIT_SUCCEEDED]
    assert hitl.approvals.get_action(action.action_id).status is ActionStatus.COMMITTED
    assert hitl.ledger.verify_chain().ok and hitl.ledger.commits_without_consumed_token() == []
    assert "TVDHD" not in hitl.ledger_dump()  # ledger stores ids and hashes, never content


# ------------------------------------------------------------------------------------------ no approval
def test_safety_nothing_is_written_before_approval(hitl) -> None:  # type: ignore[no-untyped-def]
    action = hitl.propose_ado()
    result = hitl.executor.execute(action.action_id, executor_id=EXECUTOR_ID)
    assert result.outcome == "refused" and result.error == "not_approved"
    assert hitl.backend.calls == []


def test_safety_write_server_refuses_missing_and_forged_tokens(hitl) -> None:  # type: ignore[no-untyped-def]
    payload = {"work_item_type": "Requirement", "fields": {"System.Title": "x"}}
    with pytest.raises(ToolCallError) as missing:
        hitl.ado_server.call("create_work_item", {"payload": payload, "action_id": "act_x"})
    assert _code(missing) == "unauthorized" and "missing_token" in str(missing.value)
    now = int(hitl.clock().timestamp())
    claims = ApprovalTokenClaims(iss=hitl.settings.hitl.token_issuer, sub="attacker", aud="commit-executor",
                                 jti="tok_forged", iat=now, exp=now + 600, act="act_x", pah=content_sha256(payload),
                                 tgt={}, scp=["ado_write.create_work_item"], vrf="sha256:x")
    forged = mint_token(claims, LocalEcdsaSigner(key_id=hitl.token_signer.key_id))  # right kid, wrong key
    with pytest.raises(ToolCallError) as bad:
        hitl.ado_server.call("create_work_item", {"payload": payload, "action_id": "act_x", "approval_token": forged})
    assert "bad_signature" in str(bad.value)
    assert hitl.backend.calls == []


# --------------------------------------------------------------------------------------- replay / misuse
def test_safety_token_replay_is_refused_by_ledger_and_write_server(hitl) -> None:  # type: ignore[no-untyped-def]
    action = hitl.propose_ado()
    _approve(hitl, action, PM)
    assert hitl.executor.execute(action.action_id, executor_id=EXECUTOR_ID).outcome == "committed"
    again = hitl.executor.execute(action.action_id, executor_id=EXECUTOR_ID)
    assert again.outcome == "refused" and again.error == "token_consumed" and len(hitl.backend.creates()) == 1
    with pytest.raises(ApprovalError) as replay:
        hitl.approvals.consume_token(action_id=action.action_id, consumer=EXECUTOR_ID)
    assert _code(replay) == "token_consumed"


def test_safety_write_server_allows_one_execution_per_token(hitl) -> None:  # type: ignore[no-untyped-def]
    action = hitl.propose_ado()
    _approve(hitl, action, PM)
    issued = hitl.approvals.consume_token(action_id=action.action_id, consumer=EXECUTOR_ID)
    args = {"payload": action.payload, "action_id": action.action_id, "approval_token": issued.token}
    hitl.ado_server.call("create_work_item", args | {"validate_only": True})
    hitl.ado_server.call("create_work_item", args)
    with pytest.raises(ToolCallError) as replay:
        hitl.ado_server.call("create_work_item", args)
    assert "replayed" in str(replay.value) and len(hitl.backend.creates()) == 1


def test_safety_token_bound_to_payload_and_tool(hitl) -> None:  # type: ignore[no-untyped-def]
    action = hitl.propose_ado()
    _approve(hitl, action, PM)
    issued = hitl.approvals.consume_token(action_id=action.action_id, consumer=EXECUTOR_ID)
    edited = action.payload | {"fields": {"System.Title": "Silently edited after approval"}}
    with pytest.raises(ToolCallError) as mismatch:
        hitl.ado_server.call("create_work_item", {"payload": edited, "action_id": action.action_id,
                                                  "approval_token": issued.token})
    assert "payload_mismatch" in str(mismatch.value)
    with pytest.raises(ToolCallError) as scope:
        hitl.ado_server.call("add_relations", {"payload": action.payload, "action_id": action.action_id,
                                               "approval_token": issued.token})
    assert "scope" in str(scope.value)
    assert hitl.backend.creates() == []


def test_safety_expired_token_is_refused(hitl) -> None:  # type: ignore[no-untyped-def]
    action = hitl.propose_ado()
    _approve(hitl, action, PM)
    hitl.clock.advance(minutes=hitl.settings.hitl.token_ttl_minutes + 1)
    result = hitl.executor.execute(action.action_id, executor_id=EXECUTOR_ID)
    assert result.outcome == "refused" and result.error == "token_expired"
    assert hitl.approvals.get_action(action.action_id).status is ActionStatus.EXPIRED
    assert hitl.backend.calls == []


# ------------------------------------------------------------------------------------------ propose gate
def test_safety_proposal_requires_fresh_signed_pass_report_for_exact_payload(hitl) -> None:  # type: ignore[no-untyped-def]
    draft = grounded_draft(hitl.verifier)
    payload = ado_payload(draft)
    ref = hitl.subjects.put("requirement_draft", draft)

    def propose(report, body=payload, action_type=ADO):  # type: ignore[no-untyped-def]
        return hitl.approvals.propose(action_type=action_type, target={"system": "ado"}, payload=body,
                                      draft_ref=ref, report=report, proposed_by=AGENT_ID)

    good = hitl.verify(draft, ADO, payload)
    assert good.overall is VerificationOutcome.PASS
    failing = hitl.verify(draft, ADO, payload | {"comment": "Password=hunter2"})
    with pytest.raises(ApprovalError) as not_pass:
        propose(failing, payload | {"comment": "Password=hunter2"})
    assert _code(not_pass) == "report_not_pass"
    forged = failing.model_copy(update={"overall": VerificationOutcome.PASS})
    with pytest.raises(ApprovalError) as signature:
        propose(forged, payload | {"comment": "Password=hunter2"})
    assert _code(signature) == "report_signature"
    with pytest.raises(ApprovalError) as other_payload:
        propose(good, payload | {"fields": {"System.Title": "Something else"}})
    assert _code(other_payload) == "payload_not_verified"
    display_only = hitl.verifier.verify_subject(draft, subject_type="requirement_draft")
    with pytest.raises(ApprovalError) as not_commit:
        propose(display_only)
    assert _code(not_commit) == "report_not_commit_bound"
    with pytest.raises(ApprovalError) as wrong_action:
        propose(good, action_type=ActionType.KB_PUBLISH_ARTICLE.value)
    assert _code(wrong_action) == "report_action_mismatch"
    with pytest.raises(ApprovalError) as unknown:
        propose(good, action_type="DELETE_PROJECT")
    assert _code(unknown) == "unknown_action"
    assert any(e.entry_type is LedgerEntryType.POLICY_DENIED for e in hitl.ledger.query())
    hitl.clock.advance(hours=hitl.settings.verifier.report_max_age_hours + 1)
    with pytest.raises(ApprovalError) as stale:
        propose(good)
    assert _code(stale) == "report_stale"


# ------------------------------------------------------------------------------------------- approvers
def test_safety_only_authenticated_authorized_humans_can_approve(hitl) -> None:  # type: ignore[no-untyped-def]
    action = hitl.propose_ado()
    with pytest.raises(ApprovalError) as outsider:
        _approve(hitl, action, OUTSIDER)
    assert _code(outsider) == "not_authorized"
    agent = PM.model_copy(update={"sub": AGENT_ID})
    with pytest.raises(ApprovalError) as self_approval:
        _approve(hitl, action, agent)
    assert _code(self_approval) == "separation_of_duties"
    rogue_assertion = type(hitl.identities)(LocalEcdsaSigner(key_id="iap-test")).issue(PM)
    with pytest.raises(ApprovalError) as unauthenticated:
        hitl.approvals.decide(action_id=action.action_id, decision=Decision.APPROVE, assertion=rogue_assertion)
    assert _code(unauthenticated) == "unauthenticated"
    assert hitl.approvals.get_action(action.action_id).status is ActionStatus.PENDING_APPROVAL
    assert not hitl.ledger.query(entry_types=[LedgerEntryType.TOKEN_ISSUED])


def test_safety_two_person_rule_needs_pm_and_ux(hitl) -> None:  # type: ignore[no-untyped-def]
    spec = prototype_spec()
    action = hitl.propose(spec, "prototype_spec", FIGMA, {"spec_sha256": content_sha256(spec), "reuse_ratio": 1.0})
    first = _approve(hitl, action, PM)
    assert not first.token_issued and (first.approvals, first.required) == (1, 2)
    with pytest.raises(ApprovalError) as duplicate:
        _approve(hitl, action, PM)
    assert _code(duplicate) == "duplicate_approver"
    assert not _approve(hitl, action, PM2).token_issued  # two PMs do not satisfy "PM + UX"
    assert _approve(hitl, action, UX).token_issued


def test_safety_rejection_after_approval_revokes_the_token(hitl) -> None:  # type: ignore[no-untyped-def]
    action = hitl.propose_ado()
    assert _approve(hitl, action, PM).token_issued
    hitl.approvals.decide(action_id=action.action_id, decision=Decision.REJECT, assertion=hitl.identities.issue(PM2),
                          comment="Not this quarter")
    assert hitl.approvals.get_action(action.action_id).status is ActionStatus.REJECTED
    assert hitl.ledger.query(entry_types=[LedgerEntryType.TOKEN_REVOKED], action_id=action.action_id)
    assert hitl.executor.execute(action.action_id, executor_id=EXECUTOR_ID).outcome == "refused"
    assert hitl.backend.calls == []


def test_safety_concurrent_decisions_use_optimistic_locking(hitl) -> None:  # type: ignore[no-untyped-def]
    action = hitl.propose_ado()
    with pytest.raises(ApprovalError) as conflict:
        hitl.approvals.decide(action_id=action.action_id, decision=Decision.APPROVE,
                              assertion=hitl.identities.issue(PM), expected_version=action.version + 1)
    assert _code(conflict) == "conflict"


# ------------------------------------------------------------------------------------ commit re-verification
def test_safety_commit_reverifies_and_blocks_changed_drafts(hitl) -> None:  # type: ignore[no-untyped-def]
    action = hitl.propose_ado()
    _approve(hitl, action, PM)
    ref = action.draft_ref
    subject_type, draft = hitl.subjects.get(ref)
    hitl.subjects.items[ref] = (subject_type, draft.model_copy(update={"work_item_type": "Bug"}))  # tamper
    result = hitl.executor.execute(action.action_id, executor_id=EXECUTOR_ID)
    assert result.outcome == "changes_requested" and "subject" in (result.error or "")
    assert hitl.backend.calls == []
    assert hitl.approvals.get_action(action.action_id).status is ActionStatus.CHANGES_REQUESTED


def test_safety_commit_blocked_when_evidence_is_tombstoned_after_approval(hitl) -> None:  # type: ignore[no-untyped-def]
    action = hitl.propose_ado()
    _approve(hitl, action, PM)
    hitl.store.set_signal_status(next(iter(hitl.store.signals)), SignalStatus.TOMBSTONED)
    result = hitl.executor.execute(action.action_id, executor_id=EXECUTOR_ID)
    assert result.outcome == "changes_requested" and hitl.backend.calls == []
    assert hitl.ledger.commits_without_consumed_token() == []


def test_safety_proposals_expire(hitl) -> None:  # type: ignore[no-untyped-def]
    action = hitl.propose_ado()
    hitl.clock.advance(hours=hitl.settings.hitl.proposal_ttl_hours + 1)
    assert hitl.approvals.expire_stale() == [action.action_id]
    with pytest.raises(ApprovalError) as expired:
        _approve(hitl, action, PM)
    assert _code(expired) == "not_pending"
    assert hitl.approvals.get_action(action.action_id).status is ActionStatus.EXPIRED


def test_token_guard_rejects_tokens_whose_claims_were_widened(hitl) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(TokenError):
        hitl.guard.authorize(scope="ado_write.create_work_item", payload={}, token="a.b.c", action_id="act",
                             validate_only=True)
    assert timedelta(minutes=hitl.settings.hitl.token_ttl_minutes) <= timedelta(minutes=15)
