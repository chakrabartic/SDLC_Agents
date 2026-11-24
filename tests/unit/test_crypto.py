"""Tests for the shared ES256 signing primitives (verifier reports + approval tokens)."""
from pm_agent.crypto import LocalEcdsaSigner, b64url_decode, b64url_encode, verify_es256


def test_es256_sign_and_verify_round_trip() -> None:
    signer = LocalEcdsaSigner(key_id="local-test-1")
    signature = signer.sign(b"payload")
    assert len(signature) == 64  # JWS raw r||s for P-256
    assert verify_es256(signer.public_key_pem(), b"payload", signature)
    assert not verify_es256(signer.public_key_pem(), b"tampered", signature)


def test_other_key_cannot_verify() -> None:
    a, b = LocalEcdsaSigner(key_id="a"), LocalEcdsaSigner(key_id="b")
    assert not verify_es256(b.public_key_pem(), b"x", a.sign(b"x"))


def test_b64url_round_trip_without_padding() -> None:
    raw = bytes(range(40))
    encoded = b64url_encode(raw)
    assert "=" not in encoded
    assert b64url_decode(encoded) == raw
