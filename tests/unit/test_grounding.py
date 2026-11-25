"""Tests for the Grounding/Verifier layer (docs/06): atoms, citation resolution, V1–V5, reports, redaction, dedup, policy."""
from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime

import pytest

from pm_agent.crypto import LocalEcdsaSigner
from pm_agent.grounding.atoms import AtomExtractor
from pm_agent.grounding.dedup import extract_cross_refs, jaccard_minhash
from pm_agent.grounding.evidence_store import InMemoryEvidenceStore
from pm_agent.grounding.judge import JudgeUnavailable, JudgeVerdict
from pm_agent.grounding.policy import PolicyEngine
from pm_agent.grounding.redaction import LocalRedactor
from pm_agent.grounding.reports import verify_report_signature
from pm_agent.grounding.verifier import Verifier
from pm_agent.schemas import (
    ActionType,
    CitedQuote,
    ClaimDraft,
    ClaimKind,
    DraftField,
    EvidenceChunk,
    FieldStatus,
    RequirementDraft,
    Signal,
    SignalStatus,
    SourceSystem,
    VerificationOutcome,
)
from pm_agent.settings import load_settings

PD_TEXT = ("For well WELL-0042(h), the channel TVDHD from source log TIME_RT_WITSML was not being populated "
           "into the standardized log TIME_SURFACE. Root cause: the standardization mapping file was uploaded after "
           "the channel streamed. Customer: CNE.")


class ScriptedJudge:
    """Deterministic judge for tests: entailed iff every word of the claim's key phrase appears in a quote."""

    def __init__(self, verdict: str = "entailed", fail: bool = False) -> None:
        self.verdict, self.fail, self.calls = verdict, fail, 0

    def judge(self, claim_text: str, quotes: Sequence[str]) -> JudgeVerdict:
        self.calls += 1
        if self.fail:
            raise JudgeUnavailable("quota exhausted")
        return JudgeVerdict(verdict=self.verdict, supporting_quote_index=0 if quotes else None)


@pytest.fixture(name="store")
def _store() -> InMemoryEvidenceStore:
    store = InMemoryEvidenceStore()
    signal = Signal(source_system=SourceSystem.PAGERDUTY, source_record_id="90000001", source_version="1",
                    record_type="incident", permalink="https://example.pagerduty.com/incidents/90000001",
                    captured_utc=datetime(2026, 5, 9, tzinfo=UTC), raw_text=PD_TEXT)
    store.add_signal(signal)
    store.add_chunk(EvidenceChunk(evidence_id="ev_pd_1", signal_id=signal.signal_id, seq=0, text=PD_TEXT,
                                  char_start=0, char_end=len(PD_TEXT)))
    return store


@pytest.fixture(name="settings")
def _settings():  # type: ignore[no-untyped-def]
    return load_settings(environ={"PM_AGENT_ENV": "local"})


def _verifier(store: InMemoryEvidenceStore, settings, judge: ScriptedJudge | None = None) -> Verifier:  # type: ignore[no-untyped-def]
    return Verifier(store=store, judge=judge or ScriptedJudge(), settings=settings,
                    signer=LocalEcdsaSigner(key_id="verifier-test"))


def _fact(text: str, quote: str | None = None, evidence_id: str = "ev_pd_1") -> ClaimDraft:
    cites = [CitedQuote(evidence_id=evidence_id, quote=quote)] if quote else []
    return ClaimDraft(text=text, kind=ClaimKind.FACT, citations=cites)


# ----------------------------------------------------------------------------------------------------- atoms
def test_atoms_extract_ids_numbers_and_aliased_organizations(settings) -> None:  # type: ignore[no-untyped-def]
    extractor = AtomExtractor.from_settings(settings)
    atoms = extractor.extract("Contoso Energy saw TVDHD missing on WELL-0042(h) in 2 logs")
    assert {"contoso energy", "tvdhd", "well-0042(h)", "2"} <= set(atoms)


def test_atoms_alias_supports_cne_for_contoso_energy(settings) -> None:  # type: ignore[no-untyped-def]
    extractor = AtomExtractor.from_settings(settings)
    assert extractor.unsupported("Contoso Energy reported TVDHD missing", [PD_TEXT]) == []
    assert extractor.unsupported("Northwind Offshore reported TVDHD missing", [PD_TEXT]) == ["northwind offshore"]


# ------------------------------------------------------------------------------------------- claim checks
def test_uncited_fact_is_blocked_by_v1_ac8(store, settings) -> None:  # type: ignore[no-untyped-def]
    result = _verifier(store, settings).check_claims([_fact("The mapping file was uploaded late.")])
    assert result.passed == []
    assert result.blocked[0].failed_checks == ["V1"]


def test_cited_and_entailed_fact_passes_with_resolved_span(store, settings) -> None:  # type: ignore[no-untyped-def]
    quote = "the standardization mapping file was uploaded after the channel streamed"
    result = _verifier(store, settings).check_claims([_fact("The mapping file was uploaded after the channel streamed.",
                                                            quote)])
    assert len(result.passed) == 1
    claim = result.passed[0]
    citation = claim.citations[0]
    assert PD_TEXT[citation.span.start:citation.span.end] == quote
    assert citation.permalink.endswith("/90000001")
    assert claim.verification.status.value == "pass"


def test_quote_not_in_evidence_fails_v3(store, settings) -> None:  # type: ignore[no-untyped-def]
    result = _verifier(store, settings).check_claims([_fact("Mapping was late.", "mapping arrived two days late")])
    assert result.blocked[0].failed_checks == ["V3"]


def test_unknown_or_tombstoned_evidence_fails_v2(store, settings) -> None:  # type: ignore[no-untyped-def]
    verifier = _verifier(store, settings)
    assert verifier.check_claims([_fact("x", "TVDHD", evidence_id="ev_missing")]).blocked[0].failed_checks == ["V2"]
    signal = next(iter(store.signals.values()))
    store.set_signal_status(signal.signal_id, SignalStatus.TOMBSTONED)
    assert verifier.check_claims([_fact("TVDHD", "TVDHD")]).blocked[0].failed_checks == ["V2"]


def test_atom_not_in_quote_fails_v4(store, settings) -> None:  # type: ignore[no-untyped-def]
    result = _verifier(store, settings).check_claims([_fact("Northwind Offshore lost channel TVDHD.", "the channel TVDHD")])
    assert result.blocked[0].failed_checks == ["V4"]


def test_judge_rejection_and_outage_fail_closed(store, settings) -> None:  # type: ignore[no-untyped-def]
    claim = _fact("TVDHD was not populated.", "the channel TVDHD from source log")
    assert _verifier(store, settings, ScriptedJudge("not_entailed")).check_claims([claim]).blocked[0].failed_checks \
        == ["V5"]
    assert _verifier(store, settings, ScriptedJudge(fail=True)).check_claims([claim]).blocked[0].failed_checks \
        == ["V5"]


def test_procedural_text_with_atoms_is_blocked(store, settings) -> None:  # type: ignore[no-untyped-def]
    ok = ClaimDraft(text="Draft ready for your review.", kind=ClaimKind.PROCEDURAL)
    leaky = ClaimDraft(text="Draft ready: 7 tickets affect CNE.", kind=ClaimKind.PROCEDURAL)
    result = _verifier(store, settings).check_claims([ok, leaky])
    assert [c.text for c in result.passed] == ["Draft ready for your review."]
    assert result.blocked[0].failed_checks == ["V1"]


# ------------------------------------------------------------------------------------------ subject report
def _draft(verifier: Verifier, *drafts: ClaimDraft) -> RequirementDraft:
    checked = verifier.check_claims(list(drafts))
    fields = {"summary": DraftField(status=FieldStatus.POPULATED, claims=checked.passed)} if checked.passed else {
        "summary": DraftField(status=FieldStatus.MISSING)}
    return RequirementDraft(problem_id="prb_1", work_item_type="Requirement", fields=fields)


def test_subject_report_passes_and_is_signed(store, settings) -> None:  # type: ignore[no-untyped-def]
    verifier = _verifier(store, settings)
    draft = _draft(verifier, _fact("TVDHD was not populated into TIME_SURFACE.",
                                   "the channel TVDHD from source log TIME_RT_WITSML was not being populated "
                                   "into the standardized log TIME_SURFACE"))
    report = verifier.verify_subject(draft, subject_type="requirement_draft")
    assert report.overall is VerificationOutcome.PASS
    assert verify_report_signature(report, verifier.signer.public_key_pem())


def test_subject_with_unverified_claim_fails(store, settings) -> None:  # type: ignore[no-untyped-def]
    from pm_agent.schemas import Claim  # pylint: disable=import-outside-toplevel
    verifier = _verifier(store, settings)
    smuggled = Claim(text="Twelve customers are affected.", kind=ClaimKind.FACT)  # never went through check_claims
    draft = RequirementDraft(problem_id="prb_1", work_item_type="Requirement",
                             fields={"summary": DraftField(status=FieldStatus.POPULATED, claims=[smuggled])})
    report = verifier.verify_subject(draft, subject_type="requirement_draft")
    assert report.overall is VerificationOutcome.FAIL
    assert smuggled.claim_id in report.blocked_claim_ids


def test_inference_outside_inferred_field_fails_v7(store, settings) -> None:  # type: ignore[no-untyped-def]
    from pm_agent.schemas import Claim  # pylint: disable=import-outside-toplevel
    verifier = _verifier(store, settings)
    guess = Claim(text="Users probably want a preview.", kind=ClaimKind.INFERENCE)
    draft = RequirementDraft(problem_id="prb_1", work_item_type="Requirement",
                             fields={"summary": DraftField(status=FieldStatus.POPULATED, claims=[guess])})
    report = verifier.verify_subject(draft, subject_type="requirement_draft", commit_bound=True)
    assert report.overall is VerificationOutcome.FAIL
    assert any(c.check_id == "V7" and c.status.value == "FAIL" for c in report.checks)


# ------------------------------------------------------------------------------------------------ redaction
PEM_BODY = ("MIIEowIBAAKCAQEAu1SU1LfVLPHCozMxH2Mo4lgOEePzNm0tRgeLezV6ffAt0gun\n"
            "VTLw7onLRnrq0/IzW7yWR7QkrmBL7jTKEn5u+qKhbwKfBstIs+bMY2Zkp18gnTxK\n"
            "LxoS2tFczGkPLPgizskuemMghRniWaoLcyehkd3qqGElvW/VDL5AaWTg0nLVkjRo9z+=")
PEM = f"-----BEGIN RSA PRIVATE KEY-----\n{PEM_BODY}\n-----END RSA PRIVATE KEY-----"


def test_local_redactor_removes_secrets_and_pseudonymizes_emails(settings) -> None:  # type: ignore[no-untyped-def]
    """docs/06 §5.2: a full PEM block (header, base64 body, END line) is replaced, not just its header."""
    redactor = LocalRedactor.from_settings(settings, pseudonym_key=b"test-key")
    text = f"Contact jane.doe@example.com; conn Server=db;Password=hunter2; key:\n{PEM}\nthanks"
    out = redactor.redact(text)
    assert "hunter2" not in out.text and "jane.doe@example.com" not in out.text
    assert "[REDACTED:CONNECTION_STRING_PASSWORD]" in out.text
    assert "[REDACTED:PRIVATE_KEY_BLOCK]" in out.text
    assert all(line not in out.text for line in PEM_BODY.splitlines()) and "-----END" not in out.text
    assert out.text.endswith("\nthanks") and redactor.inspect(out.text) == {}
    assert out.findings["EMAIL_ADDRESS"] == 1
    assert redactor.redact("mail jane.doe@example.com").text.split()[-1] == out.text.split()[1].rstrip(";")


# ---------------------------------------------------------------------------------------------------- dedup
def test_minhash_similarity_separates_near_duplicates() -> None:
    a = "Gamma ray image channel GR16XT fails to push depth data to rig WITSML store"
    b = "Gamma ray image channel GR16XT fails to push depth data to the rig WITSML store"
    c = "Legal tags must be applied to WODF drilling data before sharing"
    assert jaccard_minhash(a, b) > 0.6
    assert jaccard_minhash(a, c) < 0.2


def test_cross_refs_create_hard_links() -> None:
    text = ("Case CS0000123 associated; see https://dev.azure.com/org/Proj/_workitems/edit/1234567 "
            "and PD #90000002")
    refs = {(r.system.value, r.record_id) for r in extract_cross_refs(text)}
    assert {("servicenow", "CS0000123"), ("ado", "1234567"), ("pagerduty", "90000002")} <= refs


# --------------------------------------------------------------------------------------------------- policy
def test_policy_engine_denies_prohibited_and_external_outreach(settings) -> None:  # type: ignore[no-untyped-def]
    engine = PolicyEngine(settings)
    assert not engine.evaluate("PAYMENT", {}, {}).allowed
    assert not engine.evaluate(ActionType.OUTREACH_SEND_INTERNAL.value, {"to": ["x@gmail.com"]}, {}).allowed
    assert engine.evaluate(ActionType.OUTREACH_SEND_INTERNAL.value, {"to": ["sme@slb.com"]}, {}).allowed
    assert not engine.evaluate("DELETE_PROJECT", {}, {}).allowed
    global_kb = engine.evaluate(ActionType.KB_PUBLISH_ARTICLE.value,
                                {"audience": "global", "text": "Fix for CNE wells"}, {})
    assert not global_kb.allowed and "P-04" in global_kb.rule_ids
