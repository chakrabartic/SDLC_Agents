"""Regression tests for verified defects in the grounding / verification layer (docs/06 §3–§7).

Each section pins one defect so it cannot silently return: secrets surviving redaction (G1), wrong numbers passing V4
(G2), V10 failing open (G3), V5 reported without running (G4) and an oversized quote crashing a batch (G5).
"""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from pm_agent.grounding.atoms import AtomExtractor
from pm_agent.grounding.evidence_store import InMemoryEvidenceStore
from pm_agent.grounding.redaction import LocalRedactor
from pm_agent.mcp_servers.local import local_services
from pm_agent.schemas import (
    ActionType,
    CheckStatus,
    CitedQuote,
    Claim,
    ClaimDraft,
    ClaimKind,
    DraftField,
    DuplicateCandidate,
    DuplicateDecision,
    EvidenceChunk,
    FieldStatus,
    ProductProblem,
    RecordType,
    RequirementDraft,
    Signal,
    SourceSystem,
    VerificationOutcome,
    VerificationReport,
)
from pm_agent.schemas.claims import MAX_QUOTE_CHARS
from pm_agent.settings import Settings
from tests.support import (
    DLS_KEYS,
    ScriptedJudge,
    StaticNearDuplicates,
    ado_payload,
    fact,
    grounded_draft,
    local_settings,
    make_store,
    make_verifier,
    prototype_spec,
)

PEM_BODY = ("MIIEowIBAAKCAQEAu1SU1LfVLPHCozMxH2Mo4lgOEePzNm0tRgeLezV6ffAt0gun\n"
            "VTLw7onLRnrq0/IzW7yWR7QkrmBL7jTKEn5u+qKhbwKfBstIs+bMY2Zkp18gnTxK")


@pytest.fixture(name="settings")
def _settings() -> Settings:
    return local_settings()


@pytest.fixture(name="redactor")
def _redactor(settings: Settings) -> LocalRedactor:
    return LocalRedactor.from_settings(settings, pseudonym_key=b"test-key")


# ------------------------------------------------------------------------------------- G1: secrets in redaction
@pytest.mark.parametrize("label", ["RSA PRIVATE KEY", "PRIVATE KEY", "ENCRYPTED PRIVATE KEY", "OPENSSH PRIVATE KEY",
                                   "PGP PRIVATE KEY BLOCK"])
def test_private_key_blocks_are_redacted_whole(redactor: LocalRedactor, label: str) -> None:
    """The base64 body and the END line go with the header (docs/06 §5.2), whatever the key flavour."""
    text = f"before\n-----BEGIN {label}-----\n{PEM_BODY}\n-----END {label}-----\nafter"
    out = redactor.redact(text)
    assert out.text == "before\n[REDACTED:PRIVATE_KEY_BLOCK]\nafter"
    assert out.findings == {"PRIVATE_KEY_BLOCK": 1} and redactor.inspect(out.text) == {}


def test_truncated_private_key_is_redacted_to_the_end(redactor: LocalRedactor) -> None:
    """A pasted key without its END line fails safe: everything after the header is treated as key material."""
    out = redactor.redact(f"see key -----BEGIN EC PRIVATE KEY-----\n{PEM_BODY}")
    assert out.text == "see key [REDACTED:PRIVATE_KEY_BLOCK]"


def test_one_secret_pattern_cannot_break_another_patterns_delimiters(redactor: LocalRedactor) -> None:
    """``key_pwd=`` must not swallow ``-----BEGIN`` and leave the key body behind (patterns are order-independent)."""
    text = f"ssl.key_pwd=\n-----BEGIN ENCRYPTED PRIVATE KEY-----\n{PEM_BODY}\n-----END ENCRYPTED PRIVATE KEY-----"
    out = redactor.redact(text)
    assert all(line not in out.text for line in PEM_BODY.splitlines()), out.text
    assert "-----" not in out.text and redactor.inspect(out.text) == {}
    assert out.findings == {"CONNECTION_STRING_PASSWORD": 1, "PRIVATE_KEY_BLOCK": 1}


@pytest.mark.parametrize("name", [r"LEAK_\g<0>", r"LEAK_\1", "LEAK_\\", r"LEAK_\q"])
def test_secret_type_names_are_never_parsed_as_replacement_templates(name: str) -> None:
    """A configured infoType name is literal text: ``\\g<0>`` / ``\\1`` must not echo the secret back."""
    redactor = LocalRedactor({name: r"(tok)en-[0-9]{6}"}, pseudonym_key=b"k")
    out = redactor.redact("use token-123456 now")
    assert out.text == f"use [REDACTED:{name}] now" and "123456" not in out.text


def test_v8_blocks_a_private_key_in_the_target_payload_without_echoing_it(settings: Settings) -> None:
    """V8 inspects the exact payload; the report names the infoType and count, never the value."""
    store = make_store()
    verifier = make_verifier(store, settings, second_judge=ScriptedJudge(),
                             near_duplicate_checker=StaticNearDuplicates())
    draft = grounded_draft(verifier)
    payload = ado_payload(draft) | {"comment": f"-----BEGIN PRIVATE KEY-----\n{PEM_BODY}\n-----END PRIVATE KEY-----"}
    report = verifier.verify_subject(draft, subject_type="requirement_draft", commit_bound=True,
                                     target_action="ADO_CREATE_WORK_ITEM", target_payload=payload)
    v8 = report.check("V8")
    assert v8 is not None and v8.status is CheckStatus.FAIL and "PRIVATE_KEY_BLOCK" in v8.details
    assert "MIIEow" not in report.model_dump_json()


# ------------------------------------------------------------------------------------- G2: V4 atom consistency
@pytest.fixture(name="atoms")
def _atoms(settings: Settings) -> AtomExtractor:
    return AtomExtractor.from_settings(settings)


@pytest.mark.parametrize(("claim", "quote", "missing"), [
    ("Pressure dropped 12 psi.", "pressure dropped 12.5 psi", ["12"]),
    ("234 wells affected", "1,234 wells affected", ["234"]),
    ("1 well affected", "1,234 wells affected", ["1"]),
    ("Upgrade to 37.8 first", "upgrade to 37.8.87 first", ["37.8"]),
    ("WELL-0042 is down", "WELL-0042(h) is down", ["well-0042"]),
    ("Temperature fell to -5 C", "temperature fell to 5 C", ["-5"]),
    ("Temperature fell to \u22125 C", "temperature fell to 5 C", ["-5"]),  # U+2212 MINUS SIGN
    ("Error \uff11\uff12\uff10 again", "error 121 again", ["120"]),  # fullwidth digits are numbers too
    ("Retry \u0663 times", "retry 3 times", ["\u0663"]),  # other scripts' digits are atoms (fail closed)
])
def test_v4_reports_numbers_the_quote_does_not_state(atoms: AtomExtractor, claim: str, quote: str,
                                                     missing: list[str]) -> None:
    """A digit-bearing atom must be an exact atom of a quote — no prefix/sub-token matches (docs/06 §3.1)."""
    assert atoms.unsupported(claim, [quote]) == missing


@pytest.mark.parametrize(("claim", "quote"), [
    ("1,234 wells affected", "1234 wells affected"),
    ("1234 wells", "about 1,234 wells."),
    ("Error \uff11\uff12\uff10 again", "error 120 again"),
    ("Error 120", "error \uff11\uff12\uff10"),
    ("Pressure 12.5 psi", "pressure was 12.5."),
    ("Fell to -5 C", "it fell to \u22125 C"),
    ("WELL-0042(h) down", "For well WELL-0042(h), the channel"),
    ("TVDHD missing", "the channel tvdhd is empty"),  # identifiers without digits stay case-insensitive
    ("\uff23\uff2e\uff25 reported it", "Contoso Energy reported it"),  # fullwidth alias → canonical organization
])
def test_v4_accepts_the_same_atom_in_any_normal_form(atoms: AtomExtractor, claim: str, quote: str) -> None:
    """Normalisation (NFKC, thousands separators, trailing punctuation, minus signs, aliases) never blocks a match."""
    assert not atoms.unsupported(claim, [quote])


def test_text_is_nfkc_normalised_before_tokenising(atoms: AtomExtractor) -> None:
    """Fullwidth digits/letters are the same atoms as their ASCII forms (they used to yield no atoms at all)."""
    fullwidth = "Error \uff11\uff12\uff10 on \uff37\uff25\uff2c\uff2c-\uff10\uff10\uff14\uff12(h) at \uff23\uff2e\uff25"
    assert atoms.extract(fullwidth) == ["contoso energy", "120", "well-0042(h)"]


def test_fullwidth_digits_make_procedural_text_checkable(settings: Settings) -> None:
    """V1: procedural text may not carry atoms — fullwidth digits are not a way around it."""
    verifier = make_verifier(make_store(), settings)
    leaky = ClaimDraft(text="Draft ready: \uff17 tickets affect your account.", kind=ClaimKind.PROCEDURAL)
    assert verifier.check_claims([leaky]).blocked[0].failed_checks == ["V1"]


def test_v4_blocks_a_well_id_that_only_prefixes_the_cited_one(settings: Settings) -> None:
    """``WELL-0042`` is not supported by a quote about ``WELL-0042(h)`` (it may be another wellbore)."""
    verifier = make_verifier(make_store(), settings)
    claim = fact(text="WELL-0042 lost channel TVDHD.", quote="For well WELL-0042(h), the channel TVDHD")
    assert verifier.check_claims([claim]).blocked[0].failed_checks == ["V4"]


# ------------------------------------------------------------------------------------- G3: V10 near-duplicates
CANDIDATE = DuplicateCandidate(ref="prb_other", system="pm-agent", similarity=0.93, method="lexical")
ADO = ActionType.ADO_CREATE_WORK_ITEM.value


def _problem(**changes: Any) -> ProductProblem:
    return ProductProblem(title=Claim(text="t", kind=ClaimKind.INFERENCE),
                          classification=Claim(text="c", kind=ClaimKind.INFERENCE), **changes)


def _status(report: VerificationReport, check_id: str) -> CheckStatus:
    result = report.check(check_id)
    assert result is not None, check_id
    return result.status


def test_v10_honours_pm_decisions_recorded_on_a_product_problem(settings: Settings) -> None:
    """Problems keep their candidates in ``duplicates`` (not ``near_duplicates``): a PM "distinct" must count."""
    verifier = make_verifier(make_store(), settings, near_duplicate_checker=StaticNearDuplicates([CANDIDATE]))
    distinct = _problem(duplicates=[CANDIDATE.model_copy(update={"decision": DuplicateDecision.DISTINCT})])
    assert _status(verifier.verify_subject(_problem(), subject_type="product_problem"), "V10") \
        is CheckStatus.NEEDS_PM_DECISION
    assert _status(verifier.verify_subject(distinct, subject_type="product_problem"), "V10") is CheckStatus.PASS


def test_v10_reads_pending_duplicates_stored_on_a_problem_without_a_checker(settings: Settings) -> None:
    report = make_verifier(make_store(), settings).verify_subject(_problem(duplicates=[CANDIDATE]),
                                                                  subject_type="product_problem")
    assert _status(report, "V10") is CheckStatus.NEEDS_PM_DECISION
    assert report.overall is not VerificationOutcome.PASS


def test_v10_fails_closed_for_commit_bound_subjects_without_a_near_duplicate_source(settings: Settings) -> None:
    """Like V8/V9/P-07: a commit-bound subject that records duplicates cannot be cleared without a source."""
    verifier = make_verifier(make_store(), settings, second_judge=ScriptedJudge())
    draft = grounded_draft(verifier)
    draft_report = verifier.verify_subject(draft, subject_type="requirement_draft", commit_bound=True,
                                           target_action=ADO, target_payload=ado_payload(draft))
    problem_report = verifier.verify_subject(_problem(), subject_type="product_problem", commit_bound=True,
                                             target_action=ActionType.OUTREACH_SEND_INTERNAL.value,
                                             target_payload={"to": ["sme@slb.com"]})
    for report in (draft_report, problem_report):
        assert _status(report, "V10") is CheckStatus.FAIL and report.overall is VerificationOutcome.FAIL
    assert _status(verifier.verify_subject(draft, subject_type="requirement_draft"), "V10") is CheckStatus.SKIPPED


def test_v10_does_not_fail_subject_types_that_cannot_record_duplicates(settings: Settings) -> None:
    verifier = make_verifier(make_store(), settings, dls_component_keys=DLS_KEYS)
    spec = prototype_spec()
    report = verifier.verify_subject(spec, subject_type="prototype_spec", commit_bound=True,
                                     target_action=ActionType.FIGMA_RENDER_PROTOTYPE.value,
                                     target_payload={"reuse_ratio": spec.reuse_ratio})
    assert _status(report, "V10") is CheckStatus.SKIPPED


def test_local_services_verifier_has_a_near_duplicate_source(settings: Settings) -> None:
    """The local MCP composition must not hand out a verifier that fails every commit-bound draft on V10."""
    services = local_services(settings)
    assert services.verifier is not None and services.near_duplicates is not None
    draft = RequirementDraft(problem_id="prb_1", work_item_type="Requirement",
                             fields={"summary": DraftField(status=FieldStatus.MISSING)})
    report = services.verifier.verify_subject(draft, subject_type="requirement_draft", commit_bound=True,
                                              target_action=ADO, target_payload={"work_item_type": "Requirement"})
    assert _status(report, "V10") is CheckStatus.PASS


# ------------------------------------------------------------------------------- G4: V5 reported but never run
def test_v5_is_reported_skipped_when_the_judge_is_disabled(settings: Settings) -> None:
    """A signed report must not claim "N claims checked" for an entailment check that never ran."""
    off = settings.model_copy(update={"verifier": settings.verifier.model_copy(update={"judge_required": False})})
    judge = ScriptedJudge("not_entailed")
    verifier = make_verifier(make_store(), off, judge=judge)
    report = verifier.verify_subject(grounded_draft(verifier), subject_type="requirement_draft")
    v5 = report.check("V5")
    assert v5 is not None and v5.status is CheckStatus.SKIPPED and "judge_required" in v5.details
    assert judge.calls == 0 and _status(report, "V4") is CheckStatus.PASS


# ------------------------------------------------------------------------------ G5: oversized quote in a batch
def _seed(store: InMemoryEvidenceStore, evidence_id: str, text: str) -> None:
    signal = Signal(source_system=SourceSystem.ADO, source_record_id=evidence_id, source_version="1",
                    record_type=RecordType.WORK_ITEM, permalink=f"https://ado.example.invalid/{evidence_id}",
                    captured_utc=datetime(2026, 5, 9, tzinfo=UTC), raw_text=text)
    store.add_signal(signal)
    store.add_chunk(EvidenceChunk(evidence_id=evidence_id, signal_id=signal.signal_id, seq=0, text=text,
                                  char_start=0, char_end=len(text)))


def test_whitespace_expanded_quote_over_the_citation_limit_fails_v3_not_the_batch(settings: Settings) -> None:
    """A 399-char quote can match 557 source chars whitespace-insensitively; that is a V3 failure for one claim."""
    words = [f"w{i:03d}" for i in range(80)]
    quote = " ".join(words)
    assert len(quote) == 399 and len("   ".join(words)) > MAX_QUOTE_CHARS
    store = make_store()
    _seed(store, "ev_wide", "   ".join(words))
    loose = settings.model_copy(update={"verifier": settings.verifier.model_copy(update={"max_quote_chars": 1000})})
    wide = ClaimDraft(text="The export lists every column.", kind=ClaimKind.FACT,
                      citations=[CitedQuote(evidence_id="ev_wide", quote=quote)])
    for verifier in (make_verifier(store, loose), make_verifier(store, settings)):
        result = verifier.check_claims([wide, fact()])
        assert [b.failed_checks for b in result.blocked] == [["V3"]] and len(result.passed) == 1



# ------------------------------------------------------------ Judge & SDP fail-closed + Policy P-03 hardening
def test_gemini_judge_entails_and_fails_closed_on_every_error() -> None:
    from types import SimpleNamespace
    from pm_agent.grounding.judge import GeminiEntailmentJudge, JudgeUnavailable

    class _FakeClient:
        def __init__(self, reply: str | Exception) -> None:
            self._reply = reply
            self.calls: list[dict[str, Any]] = []

            class _Models:
                @staticmethod
                def generate_content(**kwargs: Any) -> SimpleNamespace:
                    self.calls.append(kwargs)
                    if isinstance(self._reply, Exception):
                        raise self._reply
                    return SimpleNamespace(text=self._reply)

            self.models = _Models()

    ok_client = _FakeClient('{"verdict": "entailed", "supporting_quote_index": 0}')
    judge = GeminiEntailmentJudge(ok_client, model="gemini-3.8-flash",
                                  model_armor_template="projects/p/locations/us-central1/templates/t1", variant="b")
    verdict = judge.judge("TVDHD is missing.", ["TVDHD is missing from TIME_SURFACE."])
    assert verdict.entailed and verdict.supporting_quote_index == 0
    assert ok_client.calls[0]["config"].model_armor_config.prompt_template_name.endswith("/t1")

    # Out-of-range supporting quote index → fail closed.
    bad_idx = GeminiEntailmentJudge(_FakeClient('{"verdict": "entailed", "supporting_quote_index": 3}'),
                                    model="gemini-3.8-flash")
    with pytest.raises(JudgeUnavailable, match="does not exist"):
        bad_idx.judge("claim", ["only one quote"])

    # Transport/API errors and invalid JSON → fail closed as JudgeUnavailable.
    for broken in (_FakeClient(RuntimeError("quota")), _FakeClient("{not json")):
        with pytest.raises(JudgeUnavailable):
            GeminiEntailmentJudge(broken, model="gemini-3.8-flash").judge("claim", ["q"])


def test_sdp_redactor_deidentifies_and_inspects_via_dlp_client() -> None:
    from types import SimpleNamespace
    from pm_agent.grounding.redaction import SdpRedactor

    class _FakeDlp:
        def __init__(self) -> None:
            self.requests: list[dict[str, Any]] = []

        def deidentify_content(self, *, request: dict[str, Any]) -> SimpleNamespace:
            self.requests.append(request)
            summary = SimpleNamespace(info_type=SimpleNamespace(name="EMAIL_ADDRESS"),
                                      results=[SimpleNamespace(count=2)])
            return SimpleNamespace(item=SimpleNamespace(value="redacted text"),
                                   overview=SimpleNamespace(transformation_summaries=[summary]))

        def inspect_content(self, *, request: dict[str, Any]) -> SimpleNamespace:
            self.requests.append(request)
            findings = [SimpleNamespace(info_type=SimpleNamespace(name="SECURITY_DATA"))]
            return SimpleNamespace(result=SimpleNamespace(findings=findings))

    client = _FakeDlp()
    sdp = SdpRedactor("proj", "us-central1", "wrapped-b64", "projects/p/locations/l/keyRings/r/cryptoKeys/k",
                      custom_infotypes={"ADO_PAT": "[a-z0-9]{52}"}, client=client)
    res = sdp.redact("contact a@b.com and c@d.com")
    assert res.text == "redacted text" and res.findings == {"EMAIL_ADDRESS": 2}
    assert sdp.inspect("raw secret") == {"SECURITY_DATA": 1}
    assert client.requests[0]["parent"] == "projects/proj/locations/us-central1"


def test_policy_p03_parses_rfc5322_recipients_and_rejects_smuggled_domains(settings: Settings) -> None:
    from pm_agent.grounding.policy import PolicyEngine

    engine = PolicyEngine(settings)
    allowed = engine.evaluate(ActionType.OUTREACH_SEND_INTERNAL.value,
                              {"to": ["Bob Builder <bob@slb.com>", "pm@sub.slb.com"]}, {})
    assert allowed.allowed

    for bad in ("bob@evil.com@slb.com", "bob%evil.com@slb.com", "bob@slb.com, mallory@evil.com",
                "evil.com", "Bob <bob@evil.com>", "bob@slb.com\n"):
        decision = engine.evaluate(ActionType.OUTREACH_SEND_INTERNAL.value, {"to": [bad]}, {})
        assert not decision.allowed and "P-03" in decision.rule_ids, bad
