"""Fuzzing the approval-token verifier (docs/07 §6): hostile input fails closed with ``TokenError`` — nothing else.

A write server must never crash (or, worse, accept) on a malformed ``approval_token``: every refusal is a typed
``TokenError`` the server turns into ``unauthorized``. The single-character mutation properties pin the encoding as
non-malleable — in particular the unused low bits of each segment's last base64url character (``qw`` and ``qx`` once
decoded to the same byte).
"""
from __future__ import annotations

import json
import string

import pytest
from hypothesis import assume, example, given
from hypothesis import strategies as st

from pm_agent.crypto import LocalEcdsaSigner, b64url_encode
from pm_agent.hitl.tokens import MAX_TOKEN_CHARS, TokenError, mint_token, verify_token
from pm_agent.schemas import ApprovalTokenClaims, content_sha256

pytestmark = pytest.mark.property

SIGNER = LocalEcdsaSigner(key_id="approval-fuzz")
KEYS = {SIGNER.key_id: SIGNER.public_key_pem()}
SCOPE = "ado_write.create_work_item"
PAYLOAD = {"work_item_type": "Requirement", "fields": {"System.Title": "TVDHD not populated"}}
NOW = 1_800_000_000
CLAIMS = ApprovalTokenClaims(iss="https://ledger.test", sub="pm-1@slb.test", aud="commit-executor", jti="tok_fuzz",
                             iat=NOW, exp=NOW + 900, act="act_fuzz", pah=content_sha256(PAYLOAD),
                             tgt={"system": "ado", "organization": "contoso", "project": "Planck"}, scp=[SCOPE],
                             vrf="sha256:abc")
TOKEN = mint_token(CLAIMS, SIGNER)
B64URL = string.ascii_letters + string.digits + "-_"
TRUSTED_HEADER = b64url_encode(json.dumps({"alg": "ES256", "typ": "JWT", "kid": SIGNER.key_id}).encode("utf-8"))

_json = st.recursive(st.none() | st.booleans() | st.integers() | st.floats(allow_nan=False) | st.text(max_size=20),
                     lambda children: st.lists(children, max_size=4)
                     | st.dictionaries(st.text(max_size=8), children, max_size=4),
                     max_leaves=12)


def _verify(token: str) -> ApprovalTokenClaims:
    return verify_token(token, keys=KEYS, audience="commit-executor", issuer="https://ledger.test", scope=SCOPE,
                        payload=PAYLOAD, action_id="act_fuzz", now=NOW + 1)


def test_positive_control_the_unmodified_token_verifies() -> None:
    assert _verify(TOKEN) == CLAIMS


@given(st.text())
@example("")
@example("..")
@example("a.b.c")
@example("YW!!Jj.YW!!Jj.YW!!Jj")
@example(TOKEN + ".")
def test_arbitrary_strings_raise_only_token_error(text: str) -> None:
    with pytest.raises(TokenError):
        _verify(text)


@given(st.tuples(*[st.text(alphabet=B64URL, max_size=120)] * 3).map(".".join))
def test_three_part_base64url_garbage_raises_only_token_error(token: str) -> None:
    with pytest.raises(TokenError):
        _verify(token)


@given(header=_json, body=st.binary(max_size=300), signature=st.binary(max_size=80))
def test_structured_forgeries_raise_only_token_error(header: object, body: bytes, signature: bytes) -> None:
    token = ".".join([b64url_encode(json.dumps(header).encode("utf-8")), b64url_encode(body),
                      b64url_encode(signature)])
    with pytest.raises(TokenError):
        _verify(token)


@given(claims=st.dictionaries(st.sampled_from(sorted(CLAIMS.model_fields)), _json, max_size=12),
       signature=st.binary(min_size=64, max_size=64))
def test_claims_are_never_trusted_before_the_signature(claims: dict[str, object], signature: bytes) -> None:
    """A trusted ``kid`` + plausible claims + a random 64-byte signature is still a forgery."""
    forged = f"{TRUSTED_HEADER}.{b64url_encode(json.dumps(CLAIMS.model_dump() | claims).encode('utf-8'))}"
    with pytest.raises(TokenError) as refused:
        _verify(f"{forged}.{b64url_encode(signature)}")
    assert refused.value.code in {"bad_signature", "malformed"}


@given(index=st.integers(min_value=0, max_value=len(TOKEN) - 1), replacement=st.characters())
def test_any_single_character_mutation_is_rejected(index: int, replacement: str) -> None:
    assume(replacement != TOKEN[index])
    with pytest.raises(TokenError):
        _verify(TOKEN[:index] + replacement + TOKEN[index + 1:])


def test_every_substitution_of_each_segments_last_character_is_rejected() -> None:
    """Exhaustive over the positions whose low bits the lenient decoder used to ignore."""
    ends = [i for i, char in enumerate(TOKEN) if i == len(TOKEN) - 1 or TOKEN[i + 1] == "."]
    accepted = []
    for index in ends:
        for replacement in B64URL:
            if replacement == TOKEN[index]:
                continue
            try:
                _verify(TOKEN[:index] + replacement + TOKEN[index + 1:])
            except TokenError:
                continue
            accepted.append((index, replacement))
    assert not accepted, f"malleable token characters: {accepted}"


def test_oversized_and_deeply_nested_tokens_are_malformed() -> None:
    nested = b64url_encode(b"[" * 20_000 + b"]" * 20_000)
    for token in (f"{nested}.e30.", "a" * (MAX_TOKEN_CHARS + 1)):
        with pytest.raises(TokenError) as refused:
            _verify(token)
        assert refused.value.code == "malformed"
