"""ES256 signing shared by verifier reports and approval tokens (docs/06 §3, docs/07 §6).

* ``LocalEcdsaSigner`` — in-process P-256 key for local runs and tests.
* ``KmsSigner`` — Cloud KMS ``EC_SIGN_P256_SHA256`` key; only the ``mcp-ledger`` / ``mcp-verifier`` service accounts
  hold ``cloudkms.signer`` on it (Terraform), so LLM-facing services can never mint tokens.
Signatures use the JWS raw ``r || s`` encoding (64 bytes for P-256).
"""

from __future__ import annotations

import base64
import hashlib
from typing import Any, Protocol

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature, encode_dss_signature

_COORD_BYTES = 32


def b64url_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def b64url_decode(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _der_to_raw(der: bytes) -> bytes:
    r, s = decode_dss_signature(der)
    return r.to_bytes(_COORD_BYTES, "big") + s.to_bytes(_COORD_BYTES, "big")


class Signer(Protocol):
    """Anything that can produce ES256 signatures and publish its public key."""

    algorithm: str

    @property
    def key_id(self) -> str: ...

    def sign(self, data: bytes) -> bytes: ...

    def public_key_pem(self) -> bytes: ...


class LocalEcdsaSigner:
    """P-256 signer with an in-memory key (local/dev only — production uses ``KmsSigner``)."""

    algorithm = "ES256"

    def __init__(self, key_id: str, private_key: ec.EllipticCurvePrivateKey | None = None) -> None:
        self._key_id = key_id
        self._key = private_key or ec.generate_private_key(ec.SECP256R1())

    @property
    def key_id(self) -> str:
        return self._key_id

    def sign(self, data: bytes) -> bytes:
        return _der_to_raw(self._key.sign(data, ec.ECDSA(hashes.SHA256())))

    def public_key_pem(self) -> bytes:
        return self._key.public_key().public_bytes(serialization.Encoding.PEM,
                                                   serialization.PublicFormat.SubjectPublicKeyInfo)


class KmsSigner:
    """Cloud KMS asymmetric signer. ``key_version_name`` = projects/…/cryptoKeyVersions/N (Terraform output)."""

    algorithm = "ES256"

    def __init__(self, key_version_name: str, client: Any | None = None) -> None:
        self._name = key_version_name
        self._client = client

    @property
    def key_id(self) -> str:
        return self._name

    def _kms(self) -> Any:
        if self._client is None:
            try:
                from google.cloud import kms  # type: ignore[attr-defined]  # pylint: disable=import-outside-toplevel
            except ImportError as exc:  # pragma: no cover - exercised only in deployed services
                raise RuntimeError("KmsSigner requires the 'google-cloud-kms' package in the deployed image") from exc
            self._client = kms.KeyManagementServiceClient()
        return self._client

    def sign(self, data: bytes) -> bytes:
        digest = hashlib.sha256(data).digest()
        response = self._kms().asymmetric_sign(request={"name": self._name, "digest": {"sha256": digest}})
        return _der_to_raw(bytes(response.signature))

    def public_key_pem(self) -> bytes:
        return str(self._kms().get_public_key(request={"name": self._name}).pem).encode("ascii")


def verify_es256(public_key_pem: bytes, data: bytes, signature: bytes) -> bool:
    """Verify a raw (r||s) ES256 signature."""
    if len(signature) != 2 * _COORD_BYTES:
        return False
    public_key = serialization.load_pem_public_key(public_key_pem)
    if not isinstance(public_key, ec.EllipticCurvePublicKey):
        return False
    der = encode_dss_signature(int.from_bytes(signature[:_COORD_BYTES], "big"),
                               int.from_bytes(signature[_COORD_BYTES:], "big"))
    try:
        public_key.verify(der, data, ec.ECDSA(hashes.SHA256()))
    except InvalidSignature:
        return False
    return True
