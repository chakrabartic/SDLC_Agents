"""De-duplication primitives (docs/06 §6): provenance hashes, cross-reference hard links, MinHash similarity."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable

from pm_agent.schemas import CrossRef, SourceSystem

_WORD = re.compile(r"[a-z0-9_]+")
_MAX64 = (1 << 64) - 1

# Versioned detectors (docs/10 §2.1). Patterns are corpus-informed; extend via config without code changes.
CROSS_REF_PATTERNS: list[tuple[str, SourceSystem, re.Pattern[str]]] = [
    ("ado_url", SourceSystem.ADO, re.compile(r"_workitems/edit/(\d{5,9})")),
    ("ado_named", SourceSystem.ADO, re.compile(r"\b(?:Bug|Requirement|User Story|Task|Initiative)\s+#?(\d{6,9})\b")),
    ("pd_hash", SourceSystem.PAGERDUTY, re.compile(r"\bPD\s*#\s*(?:Incident\s*#\s*)?(\d{6,9})\b", re.IGNORECASE)),
    ("snow_case", SourceSystem.SERVICENOW, re.compile(r"\b(CS\d{7})\b")),
    ("snow_incident", SourceSystem.SERVICENOW, re.compile(r"\b(INC\d{7})\b")),
    ("snow_ritm", SourceSystem.SERVICENOW, re.compile(r"\b(RITM\d{7})\b")),
]


def extract_cross_refs(text: str) -> list[CrossRef]:
    """Deterministic identifiers in text — each one is a hard link for correlation."""
    refs: list[CrossRef] = []
    seen: set[tuple[str, str]] = set()
    for pattern_id, system, pattern in CROSS_REF_PATTERNS:
        for match in pattern.finditer(text):
            key = (system.value, match.group(1))
            if key in seen:
                continue
            seen.add(key)
            refs.append(CrossRef(system=system, record_id=match.group(1), pattern_id=pattern_id,
                                 char_start=match.start(1), char_end=match.end(1)))
    return refs


def shingles(text: str, k: int = 3) -> set[str]:
    words = _WORD.findall(text.lower())
    if len(words) <= k:
        return {" ".join(words)} if words else set()
    return {" ".join(words[i:i + k]) for i in range(len(words) - k + 1)}


def _hash(seed: int, value: str) -> int:
    digest = hashlib.blake2b(value.encode("utf-8"), digest_size=8, salt=seed.to_bytes(16, "big")).digest()
    return int.from_bytes(digest, "big")


def minhash_signature(items: Iterable[str], num_perm: int = 256) -> list[int]:
    values = list(items)
    if not values:
        return [_MAX64] * num_perm
    return [min(_hash(seed, v) for v in values) for seed in range(num_perm)]


def jaccard_minhash(a: str, b: str, num_perm: int = 256, k: int = 3) -> float:
    """MinHash estimate of Jaccard similarity over word k-shingles (scales to the full corpus)."""
    sig_a, sig_b = minhash_signature(shingles(a, k), num_perm), minhash_signature(shingles(b, k), num_perm)
    return sum(1 for x, y in zip(sig_a, sig_b) if x == y) / num_perm


def jaccard_exact(a: str, b: str, k: int = 3) -> float:
    sa, sb = shingles(a, k), shingles(b, k)
    return 1.0 if not sa and not sb else len(sa & sb) / len(sa | sb)
