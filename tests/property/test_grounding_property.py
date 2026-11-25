"""Property-based tests over grounding, redaction, chunking, canonicalization and similarity invariants.

Every property pins a mathematical contract that example-based tests cannot exhaust (docs/06 §3–§6, docs/13 §1).
"""
from __future__ import annotations

import json
import string
import unicodedata
from datetime import UTC, datetime

import pytest
from hypothesis import assume, given
from hypothesis import strategies as st

from pm_agent.correlation.scoring import LexicalScorer
from pm_agent.grounding.atoms import AtomExtractor
from pm_agent.grounding.chunking import chunk_signal, segment
from pm_agent.grounding.dedup import jaccard_exact, jaccard_minhash
from pm_agent.grounding.redaction import LocalRedactor
from pm_agent.grounding.reports import verify_report_signature
from pm_agent.grounding.verifier import _locate  # pylint: disable=protected-access
from pm_agent.schemas import (
    Claim,
    ClaimKind,
    DraftField,
    FieldStatus,
    RecordType,
    RequirementDraft,
    Signal,
    SourceSystem,
    VerificationReport,
    canonical_json,
)
from pm_agent.schemas.subjects import subject_digest
from tests.support import local_settings, make_store, make_verifier

pytestmark = pytest.mark.property

SETTINGS = local_settings()
REDACTOR = LocalRedactor.from_settings(SETTINGS, pseudonym_key=b"pbt-key")
ATOMS = AtomExtractor.from_settings(SETTINGS)
SCORER = LexicalScorer(SETTINGS, ATOMS)
_ALNUM = string.ascii_letters + string.digits
_B64 = _ALNUM + "+/"
_SAFE_TEXT = st.text(alphabet=string.ascii_letters + string.digits + " \n.,;:!?()-", max_size=80)


# ------------------------------------------------------------------------- 1. LocalRedactor: no-leak & idempotence
_PEM_SECRET = st.builds(
    lambda label, body: f"-----BEGIN {label}-----\n{body}\n-----END {label}-----",
    st.sampled_from(["RSA PRIVATE KEY", "PRIVATE KEY", "EC PRIVATE KEY", "OPENSSH PRIVATE KEY"]),
    st.text(alphabet=_B64, min_size=24, max_size=96),
)
_EMAIL_SECRET = st.builds(
    lambda user, dom, tld: f"{user}@{dom}.{tld}",
    st.text(alphabet=string.ascii_lowercase + string.digits, min_size=3, max_size=12),
    st.text(alphabet=string.ascii_lowercase, min_size=3, max_size=10),
    st.sampled_from(["com", "org", "net", "io"]),
)
_AWS_SECRET = st.builds(lambda tail: "AKIA" + tail,
                        st.text(alphabet=string.ascii_uppercase + string.digits, min_size=16, max_size=16))
_BEARER_SECRET = st.builds(lambda tok: "Bearer " + tok, st.text(alphabet=_ALNUM + "_-", min_size=20, max_size=40))


@given(prefix=_SAFE_TEXT, secret=st.one_of(_PEM_SECRET, _EMAIL_SECRET, _AWS_SECRET, _BEARER_SECRET),
       suffix=_SAFE_TEXT)
def test_redactor_leaves_no_embedded_secret_and_is_idempotent(prefix: str, secret: str, suffix: str) -> None:
    text = f"{prefix} {secret} {suffix}"
    first = REDACTOR.redact(text)
    assert secret not in first.text
    assert REDACTOR.inspect(first.text) == {}
    second = REDACTOR.redact(first.text)
    assert second.text == first.text and second.findings == {}


# --------------------------------------------------------------------- 2. AtomExtractor: numbers & NFKC invariance
@given(n=st.integers(min_value=0, max_value=99_999), m=st.integers(min_value=0, max_value=99_999),
       frac=st.integers(min_value=1, max_value=99))
def test_atoms_reject_different_numbers_and_decimals_and_accept_self(n: int, m: int, frac: int) -> None:
    assume(n != m)
    assert str(n) in ATOMS.unsupported(f"Pressure was {n} psi.", [f"Pressure was {m} psi."])
    assert str(n) in ATOMS.unsupported(f"Pressure was {n} psi.", [f"Pressure was {n}.{frac} psi."])
    claim = f"Channel TVDHD reached {n} psi on WELL-{m:04d}."
    assert ATOMS.unsupported(claim, [claim]) == []


@given(n=st.integers(min_value=1, max_value=9999))
def test_atoms_extraction_is_nfkc_invariant(n: int) -> None:
    ascii_text = f"Depth is {n} ft"
    fullwidth_digits = "".join(chr(ord(c) + 0xFEE0) for c in str(n))
    fullwidth_text = f"Depth is {fullwidth_digits} ft"
    assert ATOMS.extract(fullwidth_text) == ATOMS.extract(ascii_text)


# ------------------------------------------------------------------ 3. Chunking, spans & whitespace-insensitive _locate
@given(text=st.text(alphabet=string.ascii_letters + string.digits + " \n\t.,;:", min_size=1, max_size=600),
       max_chars=st.integers(min_value=20, max_value=120))
def test_segment_spans_are_strictly_increasing_bounded_and_cover_non_whitespace(text: str, max_chars: int) -> None:
    spans = segment(text, max_chars=max_chars)
    prev_end = -1
    for start, end in spans:
        assert 0 <= start < end <= len(text)
        assert start >= prev_end
        assert end - start <= max_chars
        assert text[start:end] == text[start:end].strip()
        prev_end = end
    covered = "".join(text[s:e] for s, e in spans)
    assert "".join(covered.split()) == "".join(text.split())


@given(words=st.lists(st.text(alphabet=string.ascii_letters + string.digits, min_size=1, max_size=10),
                      min_size=2, max_size=10),
       seps=st.lists(st.sampled_from([" ", "  ", "\n", "\t", " \n "]), min_size=10, max_size=10))
def test_locate_re_slices_exact_source_text_and_preserves_tokens(words: list[str], seps: list[str]) -> None:
    source = "prefix " + "".join(w + s for w, s in zip(words, seps)).rstrip() + " suffix"
    query = " ".join(words)
    found = _locate(unicodedata.normalize("NFC", query), source)
    assert found is not None
    span, quote = found
    assert source[span.start:span.end] == quote
    assert quote.split() == query.split()


# -------------------------------------------------------------------- 4. canonical_json & subject_digest stability
_JSON_VAL = st.recursive(
    st.none() | st.booleans() | st.integers(min_value=-10**9, max_value=10**9) | st.text(max_size=20),
    lambda child: st.lists(child, max_size=4) | st.dictionaries(st.text(max_size=8), child, max_size=4),
    max_leaves=10,
)


@given(obj=st.dictionaries(st.text(min_size=1, max_size=8), _JSON_VAL, min_size=1, max_size=6))
def test_canonical_json_is_key_order_invariant_and_idempotent(obj: dict[str, object]) -> None:
    reversed_obj = dict(reversed(list(obj.items())))
    first = canonical_json(obj)
    assert first == canonical_json(reversed_obj)
    assert canonical_json(json.loads(first)) == first


@given(title_a=st.text(alphabet=string.ascii_letters + " ", min_size=3, max_size=40),
       title_b=st.text(alphabet=string.ascii_letters + " ", min_size=3, max_size=40))
def test_subject_digest_ignores_volatile_fields_and_detects_content_changes(title_a: str, title_b: str) -> None:
    assume(title_a.strip() and title_b.strip() and title_a != title_b)
    draft = RequirementDraft(problem_id="prb_1", work_item_type="Requirement",
                             fields={"summary": DraftField(status=FieldStatus.POPULATED,
                                                           claims=[Claim(text=title_a, kind=ClaimKind.INFERENCE)])})
    digest = subject_digest(draft)
    # Round-trip through JSON and mutate volatile bookkeeping fields → digest unchanged.
    reloaded = RequirementDraft.model_validate_json(draft.model_dump_json())
    assert subject_digest(reloaded) == digest
    assert subject_digest(draft.model_copy(update={"verification_report_id": "vr_other"})) == digest
    # Mutating any non-volatile field changes the digest.
    edited = draft.model_copy(update={"fields": {"summary": DraftField(
        status=FieldStatus.POPULATED, claims=[Claim(text=title_b, kind=ClaimKind.INFERENCE)])}})
    assert subject_digest(edited) != digest


# --------------------------------------------------------------- 5. Provenance & report signatures across JSON
@given(body=st.text(alphabet=string.ascii_letters + string.digits + " .", min_size=5, max_size=120),
       record_id=st.text(alphabet=string.digits, min_size=1, max_size=12))
def test_signal_provenance_and_chunks_survive_json_round_trip(body: str, record_id: str) -> None:
    assume(body.strip())
    signal = Signal(source_system=SourceSystem.ADO, source_record_id=record_id, source_version="1",
                    record_type=RecordType.WORK_ITEM, permalink=f"https://ado.example.invalid/{record_id}",
                    captured_utc=datetime(2026, 5, 10, tzinfo=UTC), raw_text=body)
    reloaded = Signal.model_validate_json(signal.model_dump_json())
    assert reloaded.provenance_hash == signal.provenance_hash
    chunks = chunk_signal(reloaded)
    assert chunks and all(reloaded.raw_text[c.char_start:c.char_end] == c.text for c in chunks)


def test_signed_verification_report_round_trips_and_detects_single_field_tampering() -> None:
    store = make_store()
    verifier = make_verifier(store, SETTINGS)
    draft = RequirementDraft(problem_id="prb_1", work_item_type="Requirement", fields={})
    report = verifier.verify_subject(draft, subject_type="requirement_draft")
    pem = verifier.signer.public_key_pem()
    reloaded = VerificationReport.model_validate_json(report.model_dump_json())
    assert verify_report_signature(reloaded, pem)
    assert not verify_report_signature(reloaded.model_copy(update={"subject_sha256": "sha256:" + "0" * 64}), pem)
    assert not verify_report_signature(reloaded.model_copy(update={"commit_bound": True}), pem)


# ------------------------------------------------------------------ 6. Similarity: symmetry, bounds, identity
_WORD_LIST = st.lists(st.text(alphabet=string.ascii_lowercase, min_size=4, max_size=10), min_size=4, max_size=20)


@given(words_a=_WORD_LIST, words_b=_WORD_LIST)
def test_similarity_functions_are_symmetric_bounded_and_reflexive(words_a: list[str], words_b: list[str]) -> None:
    text_a, text_b = " ".join(words_a), " ".join(words_b)
    je_ab, je_ba = jaccard_exact(text_a, text_b), jaccard_exact(text_b, text_a)
    jm_ab, jm_ba = jaccard_minhash(text_a, text_b), jaccard_minhash(text_b, text_a)
    assert je_ab == je_ba and 0.0 <= je_ab <= 1.0 and jaccard_exact(text_a, text_a) == 1.0
    assert jm_ab == jm_ba and 0.0 <= jm_ab <= 1.0 and jaccard_minhash(text_a, text_a) == 1.0
    sig_a = Signal(source_system=SourceSystem.ADO, source_record_id="1", source_version="1",
                   record_type=RecordType.WORK_ITEM, permalink="https://ado.example.invalid/1",
                   captured_utc=datetime(2026, 5, 10, tzinfo=UTC), raw_text=text_a)
    sig_b = Signal(source_system=SourceSystem.EMAIL, source_record_id="2", source_version="1",
                   record_type=RecordType.EMAIL_MESSAGE, permalink="https://mail.example.invalid/2",
                   captured_utc=datetime(2026, 5, 10, tzinfo=UTC), raw_text=text_b)
    score_ab, _ = SCORER.score(sig_a, sig_b)
    score_ba, _ = SCORER.score(sig_b, sig_a)
    assert score_ab == score_ba and 0.0 <= score_ab <= 0.99
