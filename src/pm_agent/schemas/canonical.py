"""Canonical JSON, content hashing and identifiers (docs/10 DM-3).

* ``canonical_json`` implements RFC 8785 (JCS) so the same logical object always hashes the same way —
  approvals bind to ``content_sha256`` of exactly what the PM saw.
* ``new_id`` yields monotonic ULIDs with a type prefix (``sig_…``, ``ev_…``, ``act_…``).
"""

from __future__ import annotations

import hashlib
import os
import threading
import time
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

import rfc8785
from pydantic import BaseModel

_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
_ULID_LOCK = threading.Lock()
_ulid_state = {"ms": -1, "rand": 0}


def utcnow() -> datetime:
    """Timezone-aware current UTC time."""
    return datetime.now(UTC)


def _to_jsonable(obj: Any) -> Any:
    if isinstance(obj, BaseModel):
        return obj.model_dump(mode="json")
    return obj


def canonical_json(obj: Any) -> bytes:
    """RFC 8785 canonical serialization of a JSON-compatible object or Pydantic model."""
    return bytes(rfc8785.dumps(_to_jsonable(obj)))


def sha256_hex(data: bytes | str) -> str:
    """Hex SHA-256 of bytes or UTF-8 text."""
    raw = data.encode("utf-8") if isinstance(data, str) else data
    return hashlib.sha256(raw).hexdigest()


def content_sha256(obj: Any) -> str:
    """``sha256:<hex>`` over the canonical JSON of *obj* — the hash approvals and reports bind to."""
    return "sha256:" + sha256_hex(canonical_json(obj))


def string_leaves(value: Any) -> Iterator[str]:
    """Every string inside a JSON-like value (dict values, list items) — used for scanning, never for hashing."""
    if isinstance(value, str):
        yield value
    elif isinstance(value, BaseModel):
        yield from string_leaves(value.model_dump(mode="json"))
    elif isinstance(value, dict):
        for item in value.values():
            yield from string_leaves(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from string_leaves(item)


def _encode(value: int, length: int) -> str:
    chars = []
    for _ in range(length):
        chars.append(_CROCKFORD[value & 31])
        value >>= 5
    return "".join(reversed(chars))


def new_id(prefix: str) -> str:
    """Monotonic ULID (48-bit ms timestamp + 80-bit randomness) with a type prefix, e.g. ``sig_01J9…``."""
    with _ULID_LOCK:
        now_ms = int(time.time() * 1000)
        if now_ms <= _ulid_state["ms"]:
            now_ms = _ulid_state["ms"]
            _ulid_state["rand"] = (_ulid_state["rand"] + 1) & ((1 << 80) - 1)
        else:
            _ulid_state["ms"] = now_ms
            _ulid_state["rand"] = int.from_bytes(os.urandom(10), "big") & ((1 << 79) - 1)
        value = (now_ms << 80) | _ulid_state["rand"]
    return f"{prefix}_{_encode(value, 26)}"
