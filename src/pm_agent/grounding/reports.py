"""Verification-report integrity: the signing input and ES256 signature checks (docs/06 §3, ADR-004).

Kept apart from the Verifier so the ledger (``hitl.approvals``) can check a report without loading the verifier's
judges, policies and redactors — and so the signing input is defined exactly once.
"""

from __future__ import annotations

from pm_agent.crypto import Signer, b64url_decode, b64url_encode, verify_es256
from pm_agent.schemas import VerificationReport, canonical_json


def report_signing_input(report: VerificationReport) -> bytes:
    return canonical_json(report.model_dump(mode="json", exclude={"signature"}))


def sign_report(report: VerificationReport, signer: Signer) -> VerificationReport:
    """Bind the key id first, then sign the canonical JSON of everything but the signature."""
    unsigned = report.model_copy(update={"signature": None, "signing_key_id": signer.key_id})
    return unsigned.model_copy(update={"signature": b64url_encode(signer.sign(report_signing_input(unsigned)))})


def verify_report_signature(report: VerificationReport, public_key_pem: bytes) -> bool:
    """True iff the report carries a valid ES256 signature by the holder of *public_key_pem*."""
    if not report.signature:
        return False
    try:
        signature = b64url_decode(report.signature)
    except ValueError:
        return False
    return verify_es256(public_key_pem, report_signing_input(report), signature)
