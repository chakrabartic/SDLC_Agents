"""Immutable, queryable audit ledger (docs/07 §8).

Every entry is chained: ``entry_hash = sha256(prev_hash ‖ JCS(entry without hashes))``. Stores are append-only —
the SQLite reference store enforces it with triggers, mirroring the AlloyDB ``ledger_writer`` role that holds only
``INSERT, SELECT``. Payloads carry ids, hashes and decisions — never raw content (enforced by a size guard here and
by convention in every caller).

``Ledger.record_many`` appends several entries as one all-or-nothing batch (``LedgerStore.append_many``: one SQLite
transaction; one AlloyDB transaction in production). The HITL service relies on it so that pairs such as
``DECISION_RECORDED`` + ``TOKEN_ISSUED`` or ``TOKEN_CONSUMED`` + ``COMMIT_STARTED`` (H-5) are never half-written.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
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


def _checked_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    body = dict(payload)
    if len(canonical_json(body)) > MAX_PAYLOAD_BYTES or any(len(s) > MAX_PAYLOAD_STRING for s in string_leaves(body)):
        raise ValueError("ledger payloads carry ids and hashes only, never raw content")
    return body


@dataclass(frozen=True)
class NewEntry:
    """One entry of a ``Ledger.record_many`` batch — the same fields ``Ledger.record`` takes."""

    entry_type: LedgerEntryType
    actor: Actor
    payload: Mapping[str, Any] = field(default_factory=dict)
    subject_refs: Sequence[SubjectRef] = ()
    trace_id: str | None = None


class LedgerStore(Protocol):
    """Append-only persistence. ``append``/``append_many`` must fail unless the entries extend ``head.seq`` gap-free;
    ``append_many`` stores the whole batch or nothing (one transaction)."""

    def append(self, entry: LedgerEntry) -> None: ...

    def append_many(self, entries: Sequence[LedgerEntry]) -> None: ...

    def head(self) -> LedgerEntry | None: ...

    def entries(self) -> Iterator[LedgerEntry]: ...


class InMemoryLedgerStore:
    def __init__(self) -> None:
        self.entries_by_seq: dict[int, LedgerEntry] = {}
        self._lock = threading.Lock()

    def append(self, entry: LedgerEntry) -> None:
        self.append_many([entry])

    def append_many(self, entries: Sequence[LedgerEntry]) -> None:
        """Check the whole batch first, then publish it in one step (readers never see half a batch)."""
        with self._lock:
            expected = len(self.entries_by_seq) + 1
            for offset, entry in enumerate(entries):
                if entry.seq != expected + offset:
                    raise ValueError(f"ledger sequence gap: got {entry.seq}, expected {expected + offset}")
            self.entries_by_seq.update({entry.seq: entry for entry in entries})

    def head(self) -> LedgerEntry | None:
        with self._lock:
            return self.entries_by_seq.get(len(self.entries_by_seq))

    def entries(self) -> Iterator[LedgerEntry]:
        with self._lock:
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
    """Durable reference store (local/dev). Production: AlloyDB ``hitl.ledger_entries`` with INSERT/SELECT grants.

    One connection is shared across threads, so every statement runs under a lock: readers can never observe a batch
    that ``append_many`` has started but not yet committed.
    """

    def __init__(self, path: Path | str) -> None:
        self.connection = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self.connection.executescript(_DDL)
        self._lock = threading.RLock()

    def append(self, entry: LedgerEntry) -> None:
        self.append_many([entry])

    def append_many(self, entries: Sequence[LedgerEntry]) -> None:
        """One ``BEGIN IMMEDIATE … COMMIT``: every entry must be exactly ``MAX(seq) + 1`` or nothing is stored."""
        with self._lock:
            self.connection.execute("BEGIN IMMEDIATE")
            committed = False
            try:
                for entry in entries:
                    self._insert(entry)
                self.connection.execute("COMMIT")
                committed = True
            finally:
                if not committed:  # any failure — including interrupts — rolls the whole batch back
                    self.connection.execute("ROLLBACK")

    def _insert(self, entry: LedgerEntry) -> None:
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
        with self._lock:
            row = self.connection.execute("SELECT * FROM ledger_entries ORDER BY seq DESC LIMIT 1").fetchone()
        return self._row(row) if row else None

    def entries(self) -> Iterator[LedgerEntry]:
        with self._lock:
            rows = self.connection.execute("SELECT * FROM ledger_entries ORDER BY seq").fetchall()
        return iter([self._row(row) for row in rows])


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
        return self.record_many([NewEntry(entry_type, actor, dict(payload or {}), tuple(subject_refs), trace_id)])[0]

    def record_many(self, entries: Sequence[NewEntry]) -> list[LedgerEntry]:
        """Append a batch of chained entries atomically — all of them or none — and return them in order.

        Every payload is checked before anything is written, and the store receives the whole batch in one
        ``append_many`` call (one transaction), so a failure leaves the chain exactly as it was.
        """
        payloads = [_checked_payload(item.payload) for item in entries]
        if not payloads:
            return []
        with self._lock:
            head = self._store.head()
            seq, prev = (head.seq, head.entry_hash) if head else (0, GENESIS_HASH)
            chained: list[LedgerEntry] = []
            for item, payload in zip(entries, payloads, strict=True):
                seq += 1
                draft = LedgerEntry(seq=seq, entry_id=new_id("led"), entry_type=item.entry_type,
                                    subject_refs=list(item.subject_refs), actor=item.actor, payload=payload,
                                    prev_hash=prev, entry_hash=GENESIS_HASH, recorded_utc=self._clock(),
                                    trace_id=item.trace_id)
                entry = draft.model_copy(update={"entry_hash": entry_hash(prev, _body(draft))})
                chained.append(entry)
                prev = entry.entry_hash
            self._store.append_many(chained)
            return chained

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
