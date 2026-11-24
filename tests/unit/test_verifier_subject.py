"""Subject-level verification (V0, V6–V10), re-verification and report integrity (docs/06 §3, docs/07 §5)."""
from __future__ import annotations

import pytest

from pm_agent.crypto import LocalEcdsaSigner
from pm_agent.grounding import LocalRedactor, subject_digest, verify_report_signature
from pm_agent.schemas import (
    ActionType,
    ApprovalState,
    CheckStatus,
    Citation,
    ClaimKind,
    DuplicateCandidate,
    DuplicateDecision,
    NewPattern,
    PrototypeScreen,
    PrototypeSpec,
    SignalStatus,
    Span,
    SpecNode,
    VerificationOutcome,
)
from tests.support import (
    PD_QUOTE,
    PD_TEXT,
    ScriptedJudge,
    StaticNearDuplicates,
    ado_payload,
    fact,
    grounded_draft,
    local_settings,
    make_store,
    make_verifier,
)

ADO = ActionType.ADO_CREATE_WORK_ITEM.value


@pytest.fixture(name="settings")
def _settings():  # type: ignore[no-untyped-def]
    return local_settings()


@pytest.fixture(name="store")
def _store():  # type: ignore[no-untyped-def]
    return make_store()


def _commit_report(verifier, draft, **kwargs):  # type: ignore[no-untyped-def]
    return verifier.verify_subject(draft, subject_type="requirement_draft", commit_bound=True, target_action=ADO,
                                   target_payload=ado_payload(draft), **kwargs)


def test_commit_bound_report_passes_with_full_configuration(store, settings) -> None:  # type: ignore[no-untyped-def]
    verifier = make_verifier(store, settings, second_judge=ScriptedJudge(),
                             near_duplicate_checker=StaticNearDuplicates())
    draft = grounded_draft(verifier)
    report = _commit_report(verifier, draft)
    assert report.overall is VerificationOutcome.PASS, report.checks
    assert report.target_payload_sha256 and report.commit_bound and report.target_action == ADO
    assert verify_report_signature(report, verifier.signer.public_key_pem())


def test_report_signature_detects_tampering_and_foreign_keys(store, settings) -> None:  # type: ignore[no-untyped-def]
    verifier = make_verifier(store, settings)
    report = verifier.verify_subject(grounded_draft(verifier), subject_type="requirement_draft")
    tampered = report.model_copy(update={"overall": VerificationOutcome.FAIL})
    assert not verify_report_signature(tampered, verifier.signer.public_key_pem())
    assert not verify_report_signature(report, LocalEcdsaSigner(key_id="other").public_key_pem())
    assert not verify_report_signature(report.model_copy(update={"signature": "!!not-base64!!"}),
                                       verifier.signer.public_key_pem())


def test_subject_digest_ignores_bookkeeping_but_not_content(store, settings) -> None:  # type: ignore[no-untyped-def]
    draft = grounded_draft(make_verifier(store, settings))
    booked = draft.model_copy(update={"verification_report_id": "vr_x", "approval_state": ApprovalState.APPROVED})
    assert subject_digest(booked) == subject_digest(draft)
    assert subject_digest(draft.model_copy(update={"work_item_type": "Bug"})) != subject_digest(draft)


def test_reverification_fails_when_evidence_is_tombstoned_after_approval(store, settings) -> None:  # type: ignore[no-untyped-def]
    verifier = make_verifier(store, settings)
    draft = grounded_draft(verifier)
    assert verifier.verify_subject(draft, subject_type="requirement_draft").overall is VerificationOutcome.PASS
    store.set_signal_status(next(iter(store.signals)), SignalStatus.TOMBSTONED)
    report = verifier.verify_subject(draft, subject_type="requirement_draft")
    assert report.overall is VerificationOutcome.FAIL
    assert report.check("V2").status is CheckStatus.FAIL


def _with_citation(draft, **changes):  # type: ignore[no-untyped-def]
    claim = draft.fields["summary"].claims[0]
    data = claim.citations[0].model_dump()
    data.update(changes)
    data.pop("quote_sha256")
    forged = claim.model_copy(update={"citations": [Citation.model_validate(data)]})
    field = draft.fields["summary"].model_copy(update={"claims": [forged]})
    return draft.model_copy(update={"fields": {"summary": field}})


def test_forged_span_fails_v3_and_forged_permalink_fails_v2(store, settings) -> None:  # type: ignore[no-untyped-def]
    verifier = make_verifier(store, settings)
    draft = grounded_draft(verifier)
    span = draft.fields["summary"].claims[0].citations[0].span
    shifted = _with_citation(draft, span=Span(start=span.start + 1, end=span.end + 1))
    assert verifier.verify_subject(shifted, subject_type="requirement_draft").check("V3").status is CheckStatus.FAIL
    relinked = _with_citation(draft, permalink="https://attacker.example.com/incidents/1")
    assert verifier.verify_subject(relinked, subject_type="requirement_draft").check("V2").status is CheckStatus.FAIL


def test_whitespace_variant_quote_resolves_to_exact_source_text(store, settings) -> None:  # type: ignore[no-untyped-def]
    sloppy = PD_QUOTE.replace(" from source log ", "  from source\nlog ")
    result = make_verifier(store, settings).check_claims([fact(quote=sloppy)])
    citation = result.passed[0].citations[0]
    assert citation.quote == PD_QUOTE == PD_TEXT[citation.span.start:citation.span.end]


def test_high_risk_actions_require_two_independent_judges(store, settings) -> None:  # type: ignore[no-untyped-def]
    verifier = make_verifier(store, settings, near_duplicate_checker=StaticNearDuplicates())
    report = _commit_report(verifier, grounded_draft(verifier))
    assert report.overall is VerificationOutcome.FAIL
    assert "second judge" in report.check("V5").details
    dissent = make_verifier(store, settings, second_judge=ScriptedJudge("insufficient"),
                            near_duplicate_checker=StaticNearDuplicates())
    assert _commit_report(dissent, grounded_draft(dissent)).check("V5").status is CheckStatus.FAIL


def test_v8_blocks_secrets_in_the_target_payload(store, settings) -> None:  # type: ignore[no-untyped-def]
    verifier = make_verifier(store, settings, second_judge=ScriptedJudge(),
                             near_duplicate_checker=StaticNearDuplicates())
    draft = grounded_draft(verifier)
    payload = ado_payload(draft) | {"comment": "conn Server=db;Password=hunter2"}
    report = verifier.verify_subject(draft, subject_type="requirement_draft", commit_bound=True, target_action=ADO,
                                     target_payload=payload)
    assert report.check("V8").status is CheckStatus.FAIL
    assert "hunter2" not in report.model_dump_json()


def test_commit_bound_without_redaction_inspector_fails_closed(store, settings) -> None:  # type: ignore[no-untyped-def]
    verifier = make_verifier(store, settings, with_redactor=False, second_judge=ScriptedJudge())
    assert _commit_report(verifier, grounded_draft(verifier)).check("V8").status is CheckStatus.FAIL


def test_v9_rejects_mismatched_action_and_prohibited_actions(store, settings) -> None:  # type: ignore[no-untyped-def]
    verifier = make_verifier(store, settings)
    draft = grounded_draft(verifier)
    wrong = verifier.verify_subject(draft, subject_type="requirement_draft",
                                    target_action=ActionType.KB_PUBLISH_ARTICLE.value, target_payload={})
    assert wrong.check("V9").status is CheckStatus.FAIL
    prohibited = verifier.verify_subject(draft, subject_type="requirement_draft", target_action="PAYMENT")
    assert "P-02" in prohibited.check("V9").details


def test_v10_needs_pm_decision_until_the_pm_resolves_it(store, settings) -> None:  # type: ignore[no-untyped-def]
    candidate = DuplicateCandidate(ref="ado:1234567", system="ado", similarity=0.93, method="lexical")
    verifier = make_verifier(store, settings, near_duplicate_checker=StaticNearDuplicates([candidate]))
    draft = grounded_draft(verifier)
    assert verifier.verify_subject(draft, subject_type="requirement_draft").overall \
        is VerificationOutcome.NEEDS_PM_DECISION
    decided = draft.model_copy(update={"near_duplicates": [
        candidate.model_copy(update={"decision": DuplicateDecision.DISTINCT})]})
    assert verifier.verify_subject(decided, subject_type="requirement_draft").overall is VerificationOutcome.PASS


def test_v6_aggregates_need_a_recomputation_checker(store, settings) -> None:  # type: ignore[no-untyped-def]
    aggregate = fact(kind=ClaimKind.AGGREGATE)
    without = make_verifier(store, settings)
    assert without.check_claims([aggregate]).blocked[0].failed_checks == ["V6"]
    refuted = make_verifier(store, settings, aggregate_checker=lambda claim: False)
    assert refuted.check_claims([aggregate]).blocked[0].failed_checks == ["V6"]
    checked = make_verifier(store, settings, aggregate_checker=lambda claim: True)
    assert checked.verify_subject(grounded_draft(checked, aggregate),
                                  subject_type="requirement_draft").check("V6").status is CheckStatus.PASS


def test_aggregate_counts_are_exempt_from_v4_and_left_to_v6(store, settings) -> None:  # type: ignore[no-untyped-def]
    counted = fact(text="3 incidents report TVDHD missing.", quote="the channel TVDHD", kind=ClaimKind.AGGREGATE)
    judge = ScriptedJudge("not_entailed")  # aggregates are recomputed by code, never judged
    verifier = make_verifier(store, settings, judge=judge, aggregate_checker=lambda claim: "3" in claim.text)
    assert len(verifier.check_claims([counted]).passed) == 1 and judge.calls == 0
    assert make_verifier(store, settings).check_claims([fact(text="3 incidents report TVDHD missing.",
                                                             quote="the channel TVDHD")]).blocked[0].failed_checks \
        == ["V4"]


def test_unknown_or_mismatched_subject_type_fails_v0(store, settings) -> None:  # type: ignore[no-untyped-def]
    verifier = make_verifier(store, settings)
    draft = grounded_draft(verifier)
    assert verifier.verify_subject(draft, subject_type="memo").check("V0").status is CheckStatus.FAIL
    assert verifier.verify_subject(draft, subject_type="prototype_spec").check("V0").status is CheckStatus.FAIL


def test_prototype_spec_policy_requires_dls_catalog_and_justified_patterns(store, settings) -> None:  # type: ignore[no-untyped-def]
    screen = PrototypeScreen(name="Approval", width=1440, height=900, nodes=[
        SpecNode(kind="instance", name="Primary button", component_key="dls:button/primary")])
    spec = PrototypeSpec(brief_id="brief_1", requirement_id="draft-req_1", dls_library_file_key="PLACEHOLDER:Q-03:k",
                         dls_published_version="v1", screens=[screen],
                         new_patterns=[NewPattern(name="Evidence drawer", justification=[])])
    verifier = make_verifier(store, settings, dls_component_keys={"dls:button/secondary"})
    details = verifier.verify_subject(spec, subject_type="prototype_spec").check("V9").details
    assert "no justification" in details and "not in the pinned DLS catalog" in details
    uncatalogued = make_verifier(store, settings)
    report = uncatalogued.verify_subject(spec, subject_type="prototype_spec", commit_bound=True,
                                         target_action=ActionType.FIGMA_RENDER_PROTOTYPE.value,
                                         target_payload={"reuse_ratio": spec.reuse_ratio})
    assert "Q-03" in report.check("V9").details


def test_judge_verdicts_are_reused_between_claim_and_subject_checks(store, settings) -> None:  # type: ignore[no-untyped-def]
    judge = ScriptedJudge()
    verifier = make_verifier(store, settings, judge=judge)
    verifier.verify_subject(grounded_draft(verifier), subject_type="requirement_draft")
    assert judge.calls == 1


def test_local_redactor_detects_phone_numbers_but_not_record_ids(settings) -> None:  # type: ignore[no-untyped-def]
    redactor = LocalRedactor.from_settings(settings, pseudonym_key=b"k")
    out = redactor.redact("Call +1 (555) 123-4567 about Bug 1234567 on WITSML Server 37.8.87 (PD #90000001)")
    assert "555" not in out.text and out.findings == {"PHONE_NUMBER": 1}
    assert "1234567" in out.text and "37.8.87" in out.text and "90000001" in out.text
    assert redactor.inspect("published sample-2026-11-24, due 24/11/2026") == {}  # dates are not phone numbers
