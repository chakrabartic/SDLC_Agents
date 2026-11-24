"""Approval tokens — compact JWS (ES256) over ``ApprovalTokenClaims`` (docs/07 §6).

Minted only by ``mcp-ledger`` (Cloud KMS key; no other identity holds ``cloudkms.signer``) and verified twice: by
``commit-executor`` and again by the write MCP server. Verification is strict: algorithm pinned to ES256 (no
``none``/HMAC confusion), key looked up by ``kid``, then issuer, audience, expiry, scope (the exact tool), action id
and the payload hash (``pah`` = RFC 8785 hash of exactly what the PM approved).
"""

from __future__ import annotations

import binascii
import json
import time
from collections.abc import Mapping
from typing import Any

from pydantic import ValidationError

from pm_agent.crypto import Signer, b64url_decode, b64url_encode, verify_es256
from pm_agent.schemas import ApprovalTokenClaims, canonical_json, content_sha256

ALGORITHM = "ES256"


class TokenError(Exception):
    """A refused token. ``code`` is stable and safe to log; the token itself never is."""

    def __init__(self, code: str, message: str = "") -> None:
        super().__init__(f"{code}: {message}" if message else code)
        self.code = code


def mint_token(claims: ApprovalTokenClaims, signer: Signer) -> str:
    """Compact JWS: ``b64url(header).b64url(JCS claims).b64url(r||s)``."""
    header = {"alg": ALGORITHM, "typ": "JWT", "kid": signer.key_id}
    signing_input = ".".join([b64url_encode(json.dumps(header, separators=(",", ":")).encode("utf-8")),
                              b64url_encode(canonical_json(claims))])
    return f"{signing_input}.{b64url_encode(signer.sign(signing_input.encode('ascii')))}"


def _parts(token: str) -> tuple[dict[str, Any], bytes, bytes, bytes]:
    pieces = token.split(".")
    if len(pieces) != 3:
        raise TokenError("malformed", "expected three dot-separated parts")
    try:
        header = json.loads(b64url_decode(pieces[0]))
        body = b64url_decode(pieces[1])
        signature = b64url_decode(pieces[2])
    except (ValueError, binascii.Error) as exc:
        raise TokenError("malformed", "undecodable part") from exc
    if not isinstance(header, dict):
        raise TokenError("malformed", "header is not an object")
    return header, body, signature, f"{pieces[0]}.{pieces[1]}".encode("ascii")


def verify_token(token: str, *, keys: Mapping[str, bytes], audience: str, issuer: str, scope: str, payload: Any,
                 action_id: str | None = None, now: float | None = None, leeway: int = 30) -> ApprovalTokenClaims:
    """Return the verified claims or raise ``TokenError`` with a stable reason code."""
    claims = _verified_claims(token, keys)
    current = time.time() if now is None else now
    checks = [
        (claims.iss == issuer, "wrong_issuer", ""),
        (claims.aud == audience, "wrong_audience", ""),
        (current < claims.exp + leeway, "expired", ""),
        (claims.iat <= current + leeway, "not_yet_valid", ""),
        (scope in claims.scp, "scope", f"token does not cover {scope}"),
        (action_id is None or claims.act == action_id, "action_mismatch", ""),
        (claims.pah == content_sha256(payload), "payload_mismatch", "payload differs from what the PM approved"),
    ]
    for passed, code, message in checks:
        if not passed:
            raise TokenError(code, message)
    return claims


def _verified_claims(token: str, keys: Mapping[str, bytes]) -> ApprovalTokenClaims:
    """Algorithm pin, key lookup and signature — before a single claim is trusted."""
    header, body, signature, signing_input = _parts(token)
    if header.get("alg") != ALGORITHM:
        raise TokenError("unsupported_alg", str(header.get("alg")))
    public_key = keys.get(str(header.get("kid", "")))
    if public_key is None:
        raise TokenError("unknown_key", "kid not trusted")
    if not verify_es256(public_key, signing_input, signature):
        raise TokenError("bad_signature")
    try:
        return ApprovalTokenClaims.model_validate_json(body)
    except ValidationError as exc:
        raise TokenError("malformed", "claims do not match the schema") from exc
