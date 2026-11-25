"""Tests for the shared ES256 signing primitives (verifier reports + approval tokens)."""
import hashlib
from types import SimpleNamespace
from typing import Any

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from cryptography.hazmat.primitives.asymmetric.utils import Prehashed, decode_dss_signature

from pm_agent.crypto import KmsSigner, LocalEcdsaSigner, b64url_decode, b64url_encode, verify_es256


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


# ------------------------------------------------------------------------------------- strict base64url (P2)
@pytest.mark.parametrize("text", [
    "YW!!Jj",      # reviewer's example: the lenient decoder silently dropped the '!' and returned b"abc"
    "YW Jj",       # whitespace
    "YWJj\n",      # trailing newline
    "YW+j",        # standard-alphabet characters are not base64url
    "YW/j",
    "YQ==",        # JWS base64url carries no padding (RFC 7515 §2)
    "YWJjZ",       # a length of 4n+1 can never be valid
    "YWJj\u00e9",  # non-ASCII
])
def test_b64url_decode_rejects_anything_but_canonical_base64url(text: str) -> None:
    with pytest.raises(ValueError):
        b64url_decode(text)


def test_b64url_decode_rejects_non_zero_padding_bits() -> None:
    """``qw`` and ``qx`` both carried 0xAB: without the canonical check a token's last character is malleable."""
    assert b64url_decode("qw") == b"\xab"
    with pytest.raises(ValueError):
        b64url_decode("qx")


def test_b64url_decode_accepts_the_empty_string() -> None:
    assert b64url_decode("") == b""


# ------------------------------------------------------------------------------------------- curve pinning
def _raw_signature(key: ec.EllipticCurvePrivateKey, data: bytes) -> bytes:
    r, s = decode_dss_signature(key.sign(data, ec.ECDSA(hashes.SHA256())))
    return r.to_bytes(32, "big") + s.to_bytes(32, "big")


def _pem(public_key: Any) -> bytes:
    return bytes(public_key.public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo))


def test_verify_es256_pins_the_curve_to_p256() -> None:
    """secp256k1 signatures are also 64 raw bytes — only the curve check tells them apart from ES256."""
    k1 = ec.generate_private_key(ec.SECP256K1())
    signature = _raw_signature(k1, b"payload")
    assert len(signature) == 64
    assert not verify_es256(_pem(k1.public_key()), b"payload", signature)


def test_verify_es256_rejects_non_ec_keys_and_wrong_lengths() -> None:
    rsa_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    assert not verify_es256(_pem(rsa_key.public_key()), b"x", b"\x01" * 64)
    signer = LocalEcdsaSigner(key_id="len")
    assert not verify_es256(signer.public_key_pem(), b"x", signer.sign(b"x")[:-1])


def test_local_signer_requires_a_p256_key() -> None:
    with pytest.raises(ValueError, match="P-256"):
        LocalEcdsaSigner(key_id="p384", private_key=ec.generate_private_key(ec.SECP384R1()))


# ------------------------------------------------------------------------------------------- KmsSigner (fake)
class _FakeKmsClient:
    """Stands in for ``kms.KeyManagementServiceClient``: signs the pre-hashed digest with a local P-256 key."""

    def __init__(self) -> None:
        self.key = ec.generate_private_key(ec.SECP256R1())
        self.requests: list[dict[str, Any]] = []

    def asymmetric_sign(self, request: dict[str, Any]) -> SimpleNamespace:
        self.requests.append(request)
        der = self.key.sign(request["digest"]["sha256"], ec.ECDSA(Prehashed(hashes.SHA256())))
        return SimpleNamespace(signature=der)

    def get_public_key(self, request: dict[str, Any]) -> SimpleNamespace:
        self.requests.append(request)
        return SimpleNamespace(pem=_pem(self.key.public_key()).decode("ascii"))


def test_kms_signer_signs_the_sha256_digest_and_publishes_its_pem() -> None:
    client = _FakeKmsClient()
    name = "projects/p/locations/l/keyRings/r/cryptoKeys/approval/cryptoKeyVersions/1"
    signer = KmsSigner(name, client=client)
    signature = signer.sign(b"signing-input")
    assert signer.key_id == name and signer.algorithm == "ES256" and len(signature) == 64
    assert client.requests[0] == {"name": name, "digest": {"sha256": hashlib.sha256(b"signing-input").digest()}}
    assert verify_es256(signer.public_key_pem(), b"signing-input", signature)
    assert not verify_es256(signer.public_key_pem(), b"other-input", signature)
