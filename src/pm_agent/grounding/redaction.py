"""Redaction of secrets and PII (docs/06 §5) — runs before storage and before anything leaves the perimeter.

* ``LocalRedactor`` — deterministic regex reference implementation (custom infoTypes from config + common PII);
  used locally/tests and as a second layer behind SDP.
* ``SdpRedactor`` — Sensitive Data Protection ``deidentify_content`` (``SECURITY_DATA`` → irreversible replacement;
  PII → deterministic pseudonyms with a KMS-wrapped key). Requires ``google-cloud-dlp`` in the deployed image.
"""

from __future__ import annotations

import hashlib
import hmac
import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Protocol

from pm_agent.settings import Settings

_BUILTIN_SECRETS: dict[str, str] = {
    "JSON_WEB_TOKEN": r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b",
    "AUTH_TOKEN": r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{16,}",
    "AWS_CREDENTIALS": r"\bAKIA[0-9A-Z]{16}\b",
    "GCP_API_KEY": r"\bAIza[0-9A-Za-z_-]{35}\b",
}
_PII: dict[str, str] = {
    "EMAIL_ADDRESS": r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b",
    # grouped digits only (``+1 (555) 123-4567``); bare record ids such as ``1234567`` or versions ``37.8.87`` are
    # not phone numbers, so a separator between groups is required and dots are excluded.
    "PHONE_NUMBER": r"(?<![\w.])\+?\(?\d{1,4}\)?(?:[\s-]\(?\d{1,8}\)?){2,5}(?![\w.])",
    "PERSON_MENTION": r"@[A-Z][a-z]+(?:\s[A-Z][a-z]+){0,2}",
}
_DATE_LIKE = re.compile(r"^\d{4}[-/]\d{1,2}[-/]\d{1,2}$|^\d{1,2}[-/]\d{1,2}[-/]\d{2,4}$")


def _phone_ok(value: str) -> bool:
    """7–15 digits and not a calendar date (``2026-11-24`` is not a phone number)."""
    return 7 <= sum(ch.isdigit() for ch in value) <= 15 and not _DATE_LIKE.match(value.strip())


_PII_VALIDATORS = {"PHONE_NUMBER": _phone_ok}
_TOKEN_PREFIX = {"EMAIL_ADDRESS": "EMAIL", "PHONE_NUMBER": "PHONE", "PERSON_MENTION": "PERSON"}
SECRET_TYPES = frozenset(_BUILTIN_SECRETS)
REDACTION_POLICY_VERSION = "local-redactor-1"


def _is_valid(info_type: str, value: str) -> bool:
    validator = _PII_VALIDATORS.get(info_type)
    return validator is None or bool(validator(value))


@dataclass
class RedactionResult:
    text: str
    findings: dict[str, int] = field(default_factory=dict)


class Redactor(Protocol):
    def redact(self, text: str) -> RedactionResult: ...

    def inspect(self, text: str) -> dict[str, int]: ...


class LocalRedactor:
    """Regex redactor: secrets replaced with ``[REDACTED:<TYPE>]``; PII replaced with deterministic pseudonyms."""

    policy_version = REDACTION_POLICY_VERSION

    def __init__(self, secret_patterns: dict[str, str], pseudonym_key: bytes) -> None:
        self._secrets = {name: re.compile(p) for name, p in {**_BUILTIN_SECRETS, **secret_patterns}.items()}
        self._pii = {name: re.compile(p) for name, p in _PII.items()}
        self._key = pseudonym_key
        self.secret_types = frozenset(self._secrets)

    @classmethod
    def from_settings(cls, settings: Settings, pseudonym_key: bytes) -> "LocalRedactor":
        return cls(dict(settings.grounding.custom_infotypes), pseudonym_key)

    def pseudonym(self, info_type: str, value: str) -> str:
        digest = hmac.new(self._key, value.strip().lower().encode("utf-8"), hashlib.sha256).hexdigest()
        return f"{_TOKEN_PREFIX.get(info_type, info_type)}_{digest[:6].upper()}"

    def redact(self, text: str) -> RedactionResult:
        """Replace secrets irreversibly and PII with deterministic pseudonyms; return counts, never values."""
        findings: Counter[str] = Counter()
        out = text
        for name, pattern in self._secrets.items():
            out, count = pattern.subn(f"[REDACTED:{name}]", out)
            findings[name] += count
        for name, pattern in self._pii.items():
            def _sub(match: re.Match[str], info_type: str = name) -> str:
                if not _is_valid(info_type, match.group(0)):
                    return match.group(0)
                findings[info_type] += 1
                return self.pseudonym(info_type, match.group(0))
            out = pattern.sub(_sub, out)
        return RedactionResult(text=out, findings={k: v for k, v in findings.items() if v})

    def inspect(self, text: str) -> dict[str, int]:
        found: Counter[str] = Counter()
        for name, pattern in {**self._secrets, **self._pii}.items():
            found[name] += sum(1 for m in pattern.finditer(text) if _is_valid(name, m.group(0)))
        return {k: v for k, v in found.items() if v}


class SdpRedactor:
    """Sensitive Data Protection adapter (integration-tested in stage; not exercised in unit tests)."""

    policy_version = "sdp-deidentify-1"
    SECRET_INFO_TYPES = ("SECURITY_DATA",)
    PII_INFO_TYPES = ("PERSON_NAME", "EMAIL_ADDRESS", "PHONE_NUMBER", "STREET_ADDRESS", "IP_ADDRESS")

    def __init__(self, project: str, location: str, wrapped_key_b64: str, kms_key_name: str,
                 custom_infotypes: dict[str, str] | None = None, client: Any | None = None) -> None:
        self._parent = f"projects/{project}/locations/{location}"
        self._wrapped_key = wrapped_key_b64
        self._kms_key = kms_key_name
        self._custom = custom_infotypes or {}
        self._client = client

    def _dlp(self) -> Any:
        if self._client is None:
            try:
                from google.cloud import dlp_v2  # type: ignore[attr-defined]  # pylint: disable=import-outside-toplevel
            except ImportError as exc:  # pragma: no cover
                raise RuntimeError("SdpRedactor requires the 'google-cloud-dlp' package") from exc
            self._client = dlp_v2.DlpServiceClient()
        return self._client

    def _inspect_config(self) -> dict[str, Any]:
        return {
            "info_types": [{"name": t} for t in (*self.SECRET_INFO_TYPES, *self.PII_INFO_TYPES)],
            "custom_info_types": [{"info_type": {"name": n}, "regex": {"pattern": p}} for n, p in self._custom.items()],
        }

    def redact(self, text: str) -> RedactionResult:
        """SDP ``deidentify_content``: secrets → infoType names, PII → KMS-keyed deterministic surrogates."""
        secret_names = [{"name": t} for t in self.SECRET_INFO_TYPES] + [{"name": n} for n in self._custom]
        deidentify_config = {"info_type_transformations": {"transformations": [
            {"info_types": secret_names, "primitive_transformation": {"replace_with_info_type_config": {}}},
            {"info_types": [{"name": t} for t in self.PII_INFO_TYPES],
             "primitive_transformation": {"crypto_deterministic_config": {
                 "crypto_key": {"kms_wrapped": {"wrapped_key": self._wrapped_key, "crypto_key_name": self._kms_key}},
                 "surrogate_info_type": {"name": "PERSON_TOKEN"}}}},
        ]}}
        response = self._dlp().deidentify_content(request={
            "parent": self._parent, "deidentify_config": deidentify_config,
            "inspect_config": self._inspect_config(), "item": {"value": text}})
        findings: Counter[str] = Counter()
        for summary in response.overview.transformation_summaries:
            findings[summary.info_type.name] += sum(r.count for r in summary.results)
        return RedactionResult(text=response.item.value, findings=dict(findings))

    def inspect(self, text: str) -> dict[str, int]:
        response = self._dlp().inspect_content(request={
            "parent": self._parent, "inspect_config": self._inspect_config(), "item": {"value": text}})
        found: Counter[str] = Counter(f.info_type.name for f in response.result.findings)
        return dict(found)
