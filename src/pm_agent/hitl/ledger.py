"""Immutable, queryable audit ledger (docs/07 §8).

Every entry is chained: ``entry_hash = sha256(prev_hash ‖ JCS(entry without hashes))``. Stores are append-only —
the SQLite reference store enforces it with triggers, mirroring the AlloyDB ``ledger_writer`` role that holds only
``INSERT, SELECT``. Payloads carry ids, hashes and decisions — never raw content (enforced by a size guard here and
by convention in every caller).
"""

from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Callable, Iterable, Iterator, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol

from pm_agent.schemas import (
    Actor,
    LedgerEntry,
    LedgerEntryType,
    SubjectRef,
    canonical_json,
    new_id,
    sha256_hex,
    string_leaves,
    utcnow,
)

GENESIS_HASH = "sha256:" + "0" * 64
MAX_PAYLOAD_BYTES = 4096
MAX_PAYLOAD_STRING = 512


def entry_hash(prev_hash: str, body: dict[str, Any]) -> str:
    return "sha256:" + sha256_hex(prev_hash.encode("ascii") + canonical_json(body))


def _body(entry: LedgerEntry) -> dict[str, Any]:
    return entry.model_dump(mode="json", exclude={"prev_hash", "entry_hash"})


class LedgerStore(Protocol):
    """Append-only persistence. ``append`` must fail if ``entry.seq`` is not ``head.seq + 1``."""

    def append(self, entry: LedgerEntry) -> None: ...

    def head(self) -> LedgerEntry | None: ...

    def entries(self) -> Iterator[LedgerEntry]: ...


class InMemoryLedgerStore:
    def __init__(self) -> None:
        self.entries_by_seq: dict[int, LedgerEntry] = {}

    def append(self, entry: LedgerEntry) -> None:
        expected = len(self.entries_by_seq) + 1
        if entry.seq != expected:
            raise ValueError(f"ledger sequence gap: got {entry.seq}, expected {expected}")
        self.entries_by_seq[entry.seq] = entry

    def head(self) -> LedgerEntry | None:
        return self.entries_by_seq.get(len(self.entries_by_seq))

    def entries(self) -> Iterator[LedgerEntry]:
        return iter([self.entries_by_seq[seq] for seq in sorted(self.entries_by_seq)])


_DDL = """
CREATE TABLE IF NOT EXISTS ledger_entries (
  seq INTEGER PRIMARY KEY,
  entry_id TEXT NOT NULL UNIQUE,
  entry_type TEXT NOT NULL,
  subject_refs TEXT NOT NULL,
  actor TEXT NOT NULL,
  payload TEXT NOT NULL,
  prev_hash TEXT NOT NULL,
  entry_hash TEXT NOT NULL,
  recorded_utc TEXT NOT NULL,
  trace_id TEXT
);
CREATE TRIGGER IF NOT EXISTS ledger_no_update BEFORE UPDATE ON ledger_entries
BEGIN SELECT RAISE(ABORT, 'ledger is append-only'); END;
CREATE TRIGGER IF NOT EXISTS ledger_no_delete BEFORE DELETE ON ledger_entries
BEGIN SELECT RAISE(ABORT, 'ledger is append-only'); END;
"""


class SqliteLedgerStore:
    """Durable reference store (local/dev). Production: AlloyDB ``hitl.ledger_entries`` with INSERT/SELECT grants."""

    def __init__(self, path: Path | str) -> None:
        self.connection = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self.connection.executescript(_DDL)

    def append(self, entry: LedgerEntry) -> None:
        """Insert only if ``entry.seq`` is exactly ``MAX(seq) + 1`` (gap-free, single statement)."""
        data = entry.model_dump(mode="json")
        self.connection.execute(
            "INSERT INTO ledger_entries (seq, entry_id, entry_type, subject_refs, actor, payload, prev_hash, "
            "entry_hash, recorded_utc, trace_id) "
            "SELECT ?, ?, ?, ?, ?, ?, ?, ?, ?, ? WHERE ? = (SELECT COALESCE(MAX(seq), 0) + 1 FROM ledger_entries)",
            (entry.seq, entry.entry_id, entry.entry_type.value, json.dumps(data["subject_refs"]),
             json.dumps(data["actor"]), json.dumps(data["payload"]), entry.prev_hash, entry.entry_hash,
             data["recorded_utc"], entry.trace_id, entry.seq))
        if self.connection.execute("SELECT changes()").fetchone()[0] != 1:
            raise ValueError(f"ledger sequence gap at {entry.seq}")

    def _row(self, row: Sequence[Any]) -> LedgerEntry:
        return LedgerEntry(seq=row[0], entry_id=row[1], entry_type=row[2], subject_refs=json.loads(row[3]),
                           actor=json.loads(row[4]), payload=json.loads(row[5]), prev_hash=row[6],
                           entry_hash=row[7], recorded_utc=row[8], trace_id=row[9])

    def head(self) -> LedgerEntry | None:
        row = self.connection.execute("SELECT * FROM ledger_entries ORDER BY seq DESC LIMIT 1").fetchone()
        return self._row(row) if row else None

    def entries(self) -> Iterator[LedgerEntry]:
        for row in self.connection.execute("SELECT * FROM ledger_entries ORDER BY seq"):
            yield self._row(row)


@dataclass(frozen=True)
class ChainVerification:
    ok: bool
    checked: int
    first_bad_seq: int | None = None


class Ledger:
    """The HITL Approval & Audit Ledger. Thread-safe; one writer at a time (seq is gap-free)."""

    def __init__(self, store: LedgerStore, clock: Callable[[], datetime] = utcnow) -> None:
        self._store = store
        self._clock = clock
        self._lock = threading.Lock()

    def record(self, entry_type: LedgerEntryType, *, actor: Actor, subject_refs: Iterable[SubjectRef] = (),
               payload: dict[str, Any] | None = None, trace_id: str | None = None) -> LedgerEntry:
        """Append one chained entry and return it."""
        body_payload = dict(payload or {})
        encoded = canonical_json(body_payload)
        if len(encoded) > MAX_PAYLOAD_BYTES or any(len(s) > MAX_PAYLOAD_STRING for s in string_leaves(body_payload)):
            raise ValueError("ledger payloads carry ids and hashes only, never raw content")
        with self._lock:
            head = self._store.head()
            seq = head.seq + 1 if head else 1
            prev = head.entry_hash if head else GENESIS_HASH
            draft = LedgerEntry(seq=seq, entry_id=new_id("led"), entry_type=entry_type,
                                subject_refs=list(subject_refs), actor=actor, payload=body_payload,
                                prev_hash=prev, entry_hash=GENESIS_HASH, recorded_utc=self._clock(),
                                trace_id=trace_id)
            entry = draft.model_copy(update={"entry_hash": entry_hash(prev, _body(draft))})
            self._store.append(entry)
            return entry

    def head(self) -> LedgerEntry | None:
        return self._store.head()

    def verify_chain(self) -> ChainVerification:
        """Recompute every hash from genesis (the hourly integrity job does this from the last anchor)."""
        prev, checked = GENESIS_HASH, 0
        for entry in self._store.entries():
            checked += 1
            if entry.seq != checked or entry.prev_hash != prev or entry.entry_hash != entry_hash(prev, _body(entry)):
                return ChainVerification(ok=False, checked=checked, first_bad_seq=entry.seq)
            prev = entry.entry_hash
        return ChainVerification(ok=True, checked=checked)

    def query(self, *, entry_types: Sequence[LedgerEntryType] | None = None, subject_type: str | None = None,
              subject_id: str | None = None, action_id: str | None = None) -> list[LedgerEntry]:
        """Filtered, ordered entries (AC10). Production runs the same filters as SQL / BigQuery."""
        wanted = set(entry_types) if entry_types else None
        results = []
        for entry in self._store.entries():
            if wanted is not None and entry.entry_type not in wanted:
                continue
            if action_id is not None and entry.payload.get("action_id") != action_id:
                continue
            if (subject_type or subject_id) and not any(
                    (subject_type is None or ref.type == subject_type) and (subject_id is None or ref.id == subject_id)
                    for ref in entry.subject_refs):
                continue
            results.append(entry)
        return results

    def commits_without_consumed_token(self) -> list[str]:
        """AC9 control query — must always return an empty list."""
        consumed = {e.payload.get("action_id") for e in self.query(entry_types=[LedgerEntryType.TOKEN_CONSUMED])}
        return [str(e.payload.get("action_id")) for e in self.query(entry_types=[LedgerEntryType.COMMIT_SUCCEEDED])
                if e.payload.get("action_id") not in consumed]
