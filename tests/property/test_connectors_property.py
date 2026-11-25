"""Property-based tests at the connector and webhook-gateway boundary (docs/09).

Pins:
* ``parse_utc`` never raises on arbitrary strings and always returns an aware UTC ``datetime`` or ``None``;
* every cursor decoder returns a structured value or raises only ``ConnectorError`` (never ``ValueError``,
  ``RecursionError``, ``TypeError``, …);
* ``normalize_text`` is idempotent;
* event-gateway HMAC verifiers accept the true signature and reject any 1-byte flip in body or signature.
"""
from __future__ import annotations

from datetime import UTC

import pytest
from hypothesis import assume, given
from hypothesis import strategies as st

from pm_agent.connectors.base import ConnectorError
from pm_agent.connectors.common import decode_offset_cursor, encode_offset_cursor, normalize_text, parse_utc
from pm_agent.connectors.servicenow import _decode_cursor as decode_snow_cursor  # pylint: disable=protected-access
from pm_agent.event_gateway.secrets import WebhookSecrets
from pm_agent.event_gateway.verify import hmac_sha256_hex, verify_pagerduty, verify_servicenow

pytestmark = pytest.mark.property


@given(st.text(max_size=80))
def test_parse_utc_never_raises_and_always_returns_aware_utc_or_none(raw: str) -> None:
    parsed = parse_utc(raw)
    if parsed is not None:
        assert parsed.tzinfo is UTC


@given(st.text(max_size=120))
def test_cursor_decoders_only_return_or_raise_connector_error(raw: str) -> None:
    for decoder in (decode_offset_cursor, decode_snow_cursor):
        try:
            decoder(raw)
        except ConnectorError as exc:
            assert exc.code == "validation_failed"


@given(offset=st.integers(min_value=0, max_value=10_000_000))
def test_offset_cursor_round_trips(offset: int) -> None:
    assert decode_offset_cursor(encode_offset_cursor(offset)) == offset


@given(st.text(max_size=300))
def test_normalize_text_is_idempotent(raw: str) -> None:
    once = normalize_text(raw)
    assert normalize_text(once) == once


@given(secret=st.text(min_size=8, max_size=40).filter(lambda s: "PLACEHOLDER:" not in s and s.strip() != ""),
       body=st.binary(min_size=1, max_size=256),
       flip_index=st.integers(min_value=0, max_value=255),
       flip_mask=st.integers(min_value=1, max_value=255))
def test_webhook_hmac_verifiers_accept_true_signature_and_reject_any_bit_flip(
    secret: str, body: bytes, flip_index: int, flip_mask: int
) -> None:
    secrets = WebhookSecrets(servicenow_hmac_secret=secret, pagerduty_signing_secrets=(secret,))
    sig = hmac_sha256_hex(secret, body)
    assert verify_servicenow(f"sha256={sig}", body, secrets)
    assert verify_pagerduty(f"v1={sig}", body, secrets)

    # Flip 1 byte in the body → both verifiers reject.
    pos = flip_index % len(body)
    tampered_body = bytearray(body)
    tampered_body[pos] ^= flip_mask
    assert not verify_servicenow(f"sha256={sig}", bytes(tampered_body), secrets)
    assert not verify_pagerduty(f"v1={sig}", bytes(tampered_body), secrets)

    # Flip 1 hex nibble in the signature → both verifiers reject.
    sig_pos = flip_index % len(sig)
    other_hex = "0" if sig[sig_pos] != "0" else "1"
    tampered_sig = sig[:sig_pos] + other_hex + sig[sig_pos + 1:]
    assume(tampered_sig != sig)
    assert not verify_servicenow(f"sha256={tampered_sig}", body, secrets)
    assert not verify_pagerduty(f"v1={tampered_sig}", body, secrets)
