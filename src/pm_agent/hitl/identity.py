"""Approver identity (docs/07 §5): who clicked *Approve* — verified from the IAP assertion, never from an LLM.

* ``IapJwtVerifier`` — production: validates the ``x-goog-iap-jwt-assertion`` forwarded by the PM Workbench BFF
  (ES256, issuer ``https://cloud.google.com/iap``, expected audience) with ``google.oauth2.id_token.verify_token``
  against the IAP public keys, as documented for IAP signed headers. Group membership comes from a
  ``GroupResolver`` (Workforce Identity Federation attribute mapping or Cloud Identity groups — Q-08 placeholder).
* ``LocalIdentityVerifier`` — local/dev/tests only: ES256 assertions minted by a local key. Refuses to run in
  stage/prod.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import datetime
from typing import Any, Protocol

from pydantic import ValidationError

from pm_agent.crypto import Signer, b64url_decode, b64url_encode, verify_es256
from pm_agent.placeholders import not_implemented
from pm_agent.schemas import Approver, sha256_hex, utcnow

IAP_ISSUER = "https://cloud.google.com/iap"
IAP_CERTS_URL = "https://www.gstatic.com/iap/verify/public_key"


class IdentityError(Exception):
    """The assertion could not be authenticated."""


class IdentityVerifier(Protocol):
    def verify(self, assertion: str) -> Approver: ...


class GroupResolver(Protocol):
    def groups_for(self, subject: str, email: str) -> list[str]: ...


def assertion_sha256(assertion: str) -> str:
    """Non-repudiation: the ledger keeps the hash of the exact assertion, never the assertion."""
    return "sha256:" + sha256_hex(assertion)


def _subject(value: object) -> str:
    """The approver's stable subject. Empty, blank or non-string subjects are unauthenticated: an approval must be
    attributable to exactly one person (docs/07 §5), and ``str(None)`` must never become the identity ``"None"``."""
    if not isinstance(value, str) or not value.strip():
        raise IdentityError("assertion has no subject")
    return value


class PlaceholderGroupResolver:
    """Until Q-08 is answered we do not know where SLB group membership is asserted."""

    def groups_for(self, subject: str, email: str) -> list[str]:
        del subject, email
        not_implemented("Q-08", "resolve approver IdP groups (WIF attribute mapping or Cloud Identity Groups API)")


class IapJwtVerifier:
    """Validates IAP signed headers; ``audience`` is ``/projects/<PNUM>/global/backendServices/<ID>`` (or apps)."""

    def __init__(self, *, audience: str, group_resolver: GroupResolver, idp: str = "workforce-identity",
                 request: Any | None = None) -> None:
        self._audience = audience
        self._groups = group_resolver
        self._idp = idp
        self._request = request

    def verify(self, assertion: str) -> Approver:
        """Signature, issuer, audience and expiry via google-auth; then group resolution."""
        from google.auth.transport import requests as google_requests  # pylint: disable=import-outside-toplevel
        from google.oauth2 import id_token  # pylint: disable=import-outside-toplevel

        try:
            claims = id_token.verify_token(assertion, self._request or google_requests.Request(),
                                           audience=self._audience, certs_url=IAP_CERTS_URL)
        except Exception as exc:  # pylint: disable=broad-exception-caught  # any failure = unauthenticated
            raise IdentityError("IAP assertion rejected") from exc
        if claims.get("iss") != IAP_ISSUER:
            raise IdentityError("unexpected issuer")
        subject, email = _subject(claims.get("sub")), str(claims.get("email") or "")
        return Approver(sub=subject, email=email, idp=self._idp, groups=self._groups.groups_for(subject, email))


class LocalIdentityVerifier:
    """Development stand-in for IAP: short-lived ES256 assertions over an ``Approver`` (never in stage/prod)."""

    def __init__(self, signer: Signer, *, audience: str = "pm-workbench-local",
                 clock: Callable[[], datetime] = utcnow) -> None:
        self._signer = signer
        self._public_key = signer.public_key_pem()
        self._audience = audience
        self._clock = clock

    def issue(self, approver: Approver, ttl_seconds: int = 300) -> str:
        now = int(self._clock().timestamp())
        body = {"aud": self._audience, "iat": now, "exp": now + ttl_seconds,
                "approver": approver.model_dump(mode="json")}
        signing_input = b64url_encode(json.dumps(body, sort_keys=True).encode("utf-8"))
        return f"{signing_input}.{b64url_encode(self._signer.sign(signing_input.encode('ascii')))}"

    def verify(self, assertion: str) -> Approver:
        """Signature, audience and validity window; raises ``IdentityError`` on any problem."""
        try:
            signing_input, signature = assertion.split(".")
            if not verify_es256(self._public_key, signing_input.encode("ascii"), b64url_decode(signature)):
                raise IdentityError("bad signature")
            body = json.loads(b64url_decode(signing_input))
            now = int(self._clock().timestamp())
            if body.get("aud") != self._audience or not body.get("iat", now + 1) <= now < body.get("exp", 0):
                raise IdentityError("assertion expired or for another audience")
            approver = Approver.model_validate(body["approver"])
        except (ValueError, KeyError, ValidationError) as exc:
            raise IdentityError("malformed assertion") from exc
        _subject(approver.sub)
        return approver
