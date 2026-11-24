"""Constant-time webhook verification — pure functions over header values and the RAW request body (docs/09 §9).

Rules shared by every vendor:

* Comparison uses ``hmac.compare_digest`` on UTF-8 bytes (timing-safe; arbitrary input never raises).
* A secret that is empty or still a placeholder is *not configured*: the check returns ``False`` without hashing or
  comparing anything (``secrets.is_configured``) — the placeholder token is public.
* HMACs are computed over the exact bytes received, before any JSON parsing.

The ADO and ServiceNow header names are this project's own contract (configured on the vendor side, UNCONFIRMED until
the M1 connector spike); PagerDuty, Graph and Figma follow vendor documentation from prior knowledge [K].
"""

from __future__ import annotations

import base64
import hashlib
import hmac

from pm_agent.event_gateway.secrets import WebhookSecrets, is_configured

ADO_SHARED_SECRET_HEADER = "X-PM-Agent-Webhook-Secret"
SERVICENOW_SIGNATURE_HEADER = "X-ServiceNow-Signature"
PAGERDUTY_SIGNATURE_HEADER = "X-PagerDuty-Signature"

_SHA256_PREFIX = "sha256="


def constant_time_equals(provided: str, expected: str) -> bool:
    """Timing-safe string equality over UTF-8 bytes."""
    return hmac.compare_digest(provided.encode("utf-8"), expected.encode("utf-8"))


def hmac_sha256_hex(secret: str, body: bytes) -> str:
    """Lower-case hex HMAC-SHA256 of *body* keyed with *secret*."""
    return hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()


def verify_ado(authorization: str | None, shared_secret: str | None, secrets: WebhookSecrets) -> bool:
    """ADO service hooks: ``Authorization: Basic b64(user:password)`` *or* the ``X-PM-Agent-Webhook-Secret`` header.

    Each mechanism counts only when its own secret(s) are configured; the scheme name is case-insensitive (RFC 7617).
    """
    basic_ok = False
    if (authorization is not None and is_configured(secrets.ado_basic_username)
            and is_configured(secrets.ado_basic_password)):
        scheme, _, token = authorization.strip().partition(" ")
        credentials = f"{secrets.ado_basic_username}:{secrets.ado_basic_password}".encode("utf-8")
        basic_ok = constant_time_equals(token.strip(), base64.b64encode(credentials).decode("ascii"))
        basic_ok = basic_ok and scheme.lower() == "basic"
    shared_ok = (shared_secret is not None and is_configured(secrets.ado_shared_secret)
                 and constant_time_equals(shared_secret, secrets.ado_shared_secret))
    return basic_ok or shared_ok


def verify_servicenow(signature: str | None, body: bytes, secrets: WebhookSecrets) -> bool:
    """ServiceNow outbound REST: ``X-ServiceNow-Signature: [sha256=]<hex HMAC-SHA256(secret, raw body)>``."""
    if signature is None or not is_configured(secrets.servicenow_hmac_secret):
        return False
    provided = signature.strip()
    if provided[:len(_SHA256_PREFIX)].lower() == _SHA256_PREFIX:
        provided = provided[len(_SHA256_PREFIX):].strip()
    return constant_time_equals(provided.lower(), hmac_sha256_hex(secrets.servicenow_hmac_secret, body))


def pagerduty_v1_signatures(header: str) -> list[str]:
    """The ``v1=<hex>`` entries of an ``X-PagerDuty-Signature`` header (comma-separated; other schemes ignored)."""
    signatures: list[str] = []
    for entry in header.split(","):
        scheme, separator, value = entry.strip().partition("=")
        if separator and scheme.strip().lower() == "v1" and value.strip():
            signatures.append(value.strip().lower())
    return signatures


def verify_pagerduty(signature_header: str | None, body: bytes, secrets: WebhookSecrets) -> bool:
    """PagerDuty Webhooks v3: any listed ``v1`` signature equals HMAC-SHA256(any configured secret, raw body).

    Several secrets may be configured and several signatures sent while a signing secret is being rotated.
    """
    if signature_header is None:
        return False
    expected = [hmac_sha256_hex(secret, body) for secret in secrets.pagerduty_signing_secrets if is_configured(secret)]
    matched = False
    for provided in pagerduty_v1_signatures(signature_header):
        for candidate in expected:
            matched |= constant_time_equals(provided, candidate)
    return matched


def verify_msgraph_client_state(client_state: object, secrets: WebhookSecrets) -> bool:
    """Microsoft Graph change / lifecycle notification item: ``clientState`` equals the configured value."""
    return (isinstance(client_state, str) and is_configured(secrets.msgraph_client_state)
            and constant_time_equals(client_state, secrets.msgraph_client_state))


def verify_figma_passcode(passcode: object, secrets: WebhookSecrets) -> bool:
    """Figma webhooks v2: the body field ``passcode`` equals the configured passcode."""
    return (isinstance(passcode, str) and is_configured(secrets.figma_passcode)
            and constant_time_equals(passcode, secrets.figma_passcode))
