"""Contract tests for the canonical data model (docs/10)."""
import pytest
from pydantic import ValidationError

from pm_agent.schemas import (
    ActionType,
    ApprovalTokenClaims,
    Claim,
    ClaimKind,
    DraftField,
    FieldStatus,
    ProposedAction,
    Signal,
    SourceSystem,
    canonical_json,
    content_sha256,
    new_id,
)


def _signal(**overrides: object) -> Signal:
    base: dict[str, object] = {
        "source_system": SourceSystem.PAGERDUTY,
        "source_record_id": "90000001",
        "source_version": "log-1",
        "record_type": "incident",
        "permalink": "https://example.pagerduty.com/incidents/90000001",
        "captured_utc": "2026-05-09T02:46:00Z",
        "raw_text": "Standardization mapping file was uploaded after channel streamed.",
    }
    base.update(overrides)
    return Signal.model_validate(base)


def test_canonical_json_is_key_order_independent() -> None:
    assert canonical_json({"b": 1, "a": [2, {"d": 3, "c": 4}]}) == canonical_json({"a": [2, {"c": 4, "d": 3}], "b": 1})
    digest = content_sha256({"a": 1})
    assert digest.startswith("sha256:") and len(digest) == len("sha256:") + 64


def test_ids_are_prefixed_and_time_ordered() -> None:
    first, second = new_id("sig"), new_id("sig")
    assert first.startswith("sig_") and len(first) == len("sig_") + 26
    assert first < second


def test_signal_requires_permalink_for_provenance() -> None:
    with pytest.raises(ValidationError):
        _signal(permalink="")


def test_signal_provenance_hash_tracks_content() -> None:
    a, b = _signal(), _signal()
    assert a.provenance_hash == b.provenance_hash
    assert _signal(raw_text="different text").provenance_hash != a.provenance_hash
    assert a.signal_id.startswith("sig_")


@pytest.mark.parametrize("kind,needs", [
    (ClaimKind.FACT, True), (ClaimKind.CLASSIFICATION, True), (ClaimKind.AGGREGATE, True),
    (ClaimKind.PM_JUDGMENT, True), (ClaimKind.INFERENCE, False), (ClaimKind.PROCEDURAL, False)])
def test_claim_kinds_declare_citation_requirement(kind: ClaimKind, needs: bool) -> None:
    assert Claim(text="x", kind=kind).requires_citation is needs


def test_draft_field_status_must_match_claims() -> None:
    claim = Claim(text="PMs need the mapping before channels arrive.", kind=ClaimKind.INFERENCE)
    with pytest.raises(ValidationError):
        DraftField(status=FieldStatus.MISSING, claims=[claim])
    with pytest.raises(ValidationError):
        DraftField(status=FieldStatus.POPULATED, claims=[])
    assert DraftField(status=FieldStatus.MISSING).claims == []


def test_proposed_action_binds_payload_hash() -> None:
    action = ProposedAction(action_type=ActionType.ADO_CREATE_WORK_ITEM, target={"system": "ado"},
                            payload={"title": "x"}, draft_ref="draft-req-1", verification_report_id="vr_1",
                            proposed_by="principal://agents/pm-agent-core")
    assert action.payload_sha256 == content_sha256({"title": "x"})
    with pytest.raises(ValidationError):
        ProposedAction(action_type=ActionType.ADO_CREATE_WORK_ITEM, target={"system": "ado"},
                       payload={"title": "x"}, payload_sha256="sha256:" + "0" * 64, draft_ref="d",
                       verification_report_id="vr_1", proposed_by="p")


def test_action_catalog_is_closed() -> None:
    with pytest.raises(ValueError):
        ActionType("DELETE_EVERYTHING")
    assert {a.value for a in ActionType} == {
        "ADO_CREATE_WORK_ITEM", "ADO_ADD_LINKS", "KB_PUBLISH_ARTICLE", "FIGMA_RENDER_PROTOTYPE",
        "FIGMA_POST_COMMENT", "PROTOTYPE_RELEASE_INTERNAL", "OUTREACH_SEND_INTERNAL", "ROADMAP_TRACE_LINK"}


def test_approval_token_claims_shape() -> None:
    claims = ApprovalTokenClaims(iss="https://ledger", sub="principal://pm", aud="commit-executor", jti="tok_1",
                                 iat=1, exp=901, act="act_1", pah="sha256:" + "a" * 64,
                                 tgt={"system": "ado"}, scp=["create_work_item"], vrf="sha256:" + "b" * 64)
    assert claims.exp - claims.iat == 900
    with pytest.raises(ValidationError):
        ApprovalTokenClaims(iss="i", sub="s", aud="a", jti="j", iat=10, exp=5, act="a", pah="p", tgt={},
                            scp=["x"], vrf="v")
