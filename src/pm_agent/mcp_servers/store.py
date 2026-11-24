"""``mcp-store`` backends: evidence index, versioned drafts, trace links and named analytics queries (docs/10 §7).

* ``InMemoryStoreService`` — reference implementation over ``InMemoryEvidenceStore`` + a ``SubjectStore``.
  ``put_signals`` is idempotent (deterministic ids) and enforces DM-5: a signal whose text still contains anything the
  redactor detects is rejected, never stored.
* ``AlloyDbStoreService`` — production adapter (AlloyDB AI: pgvector + ScaNN + tsvector); every call names Q-10 until
  the SLB projects/instances exist.
"""

from __future__ import annotations

import threading
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from typing import Any, NoReturn

from pm_agent.grounding.chunking import chunk_signal
from pm_agent.grounding.evidence_store import InMemoryEvidenceStore
from pm_agent.grounding.redaction import Redactor
from pm_agent.grounding.verifier import SUBJECT_TYPES, subject_digest
from pm_agent.hitl.subjects import InMemorySubjectStore, SubjectStore
from pm_agent.placeholders import not_implemented
from pm_agent.schemas import Signal, SignalStatus, TraceLink

NamedQuery = Callable[[InMemoryEvidenceStore, Mapping[str, Any]], list[dict[str, Any]]]


def _active(store: InMemoryEvidenceStore) -> list[Signal]:
    return [s for s in store.signals.values() if s.status is SignalStatus.ACTIVE]


def _brief(signal: Signal) -> dict[str, Any]:
    return {"signal_id": signal.signal_id, "source_system": signal.source_system.value,
            "source_record_id": signal.source_record_id, "record_type": signal.record_type.value,
            "permalink": signal.permalink, "captured_utc": signal.captured_utc.isoformat()}


def _counts_by_source(store: InMemoryEvidenceStore, params: Mapping[str, Any]) -> list[dict[str, Any]]:
    del params
    counts = Counter(s.source_system.value for s in _active(store))
    return [{"source_system": system, "count": count} for system, count in sorted(counts.items())]


def _by_customer(store: InMemoryEvidenceStore, params: Mapping[str, Any]) -> list[dict[str, Any]]:
    customer = str(params.get("customer") or "")
    return [_brief(s) for s in _active(store) if customer and s.customer.name == customer]


def _by_product(store: InMemoryEvidenceStore, params: Mapping[str, Any]) -> list[dict[str, Any]]:
    product = str(params.get("product") or "")
    return [_brief(s) for s in _active(store) if product and s.product == product]


def _referencing(store: InMemoryEvidenceStore, params: Mapping[str, Any]) -> list[dict[str, Any]]:
    system, record_id = str(params.get("system") or ""), str(params.get("record_id") or "")
    return [_brief(s) for s in _active(store)
            if any(r.system.value == system and r.record_id == record_id for r in s.cross_refs)]


NAMED_QUERIES: dict[str, NamedQuery] = {
    "signal_counts_by_source": _counts_by_source,
    "signals_by_customer": _by_customer,
    "signals_by_product": _by_product,
    "signals_referencing": _referencing,
}


class InMemoryStoreService:
    def __init__(self, evidence: InMemoryEvidenceStore, subjects: SubjectStore | None = None,
                 redactor: Redactor | None = None) -> None:
        self.evidence = evidence
        self.subjects = subjects or InMemorySubjectStore()
        self._redactor = redactor
        self._links: dict[str, TraceLink] = {}
        self._lock = threading.Lock()

    def put_signals(self, signals: Sequence[Signal]) -> dict[str, list[str]]:
        """Chunk and index new signal versions; known versions are skipped, unredacted ones rejected (DM-5)."""
        stored: list[str] = []
        duplicates: list[str] = []
        rejected: list[str] = []
        with self._lock:
            for signal in signals:
                if self.evidence.get_signal(signal.signal_id) is not None or signal.signal_id in stored:
                    duplicates.append(signal.signal_id)
                    continue
                if self._redactor is not None and self._redactor.inspect(f"{signal.title or ''}\n{signal.raw_text}"):
                    rejected.append(signal.signal_id)
                    continue
                self.evidence.add_signal(signal)
                for chunk in chunk_signal(signal):
                    self.evidence.add_chunk(chunk)
                stored.append(signal.signal_id)
        return {"stored": stored, "duplicates": duplicates, "rejected": rejected}

    def evidence_search(self, query: str, top_k: int = 8) -> list[dict[str, Any]]:
        """Lexical stand-in for hybrid retrieval; every hit carries its citable evidence id and permalink."""
        hits = []
        for chunk in self.evidence.search(query, top_k=top_k):
            signal = self.evidence.get_signal(chunk.signal_id)
            if signal is None:
                continue
            hits.append({"evidence_id": chunk.evidence_id, "signal_id": signal.signal_id, "seq": chunk.seq,
                         "text": chunk.text, "char_start": chunk.char_start, "char_end": chunk.char_end,
                         "source_system": signal.source_system.value, "source_record_id": signal.source_record_id,
                         "permalink": signal.permalink})
        return hits

    def save_draft(self, subject_type: str, draft: Mapping[str, Any]) -> dict[str, str]:
        model_cls = SUBJECT_TYPES.get(subject_type)
        if model_cls is None:
            raise ValueError(f"unknown subject type {subject_type!r}")
        subject = model_cls.model_validate(dict(draft))
        return {"draft_ref": self.subjects.put(subject_type, subject), "content_sha256": subject_digest(subject)}

    def trace_query(self, *, from_id: str | None = None, to_id: str | None = None, link_type: str | None = None,
                    status: str | None = None) -> list[dict[str, Any]]:
        with self._lock:
            links = list(self._links.values())
        return [link.model_dump(mode="json") for link in links
                if (from_id is None or link.from_id == from_id) and (to_id is None or link.to_id == to_id)
                and (link_type is None or link.link_type.value == link_type)
                and (status is None or link.status == status)]

    def trace_propose_links(self, links: Sequence[Mapping[str, Any]]) -> list[str]:
        """Store agent-proposed links (always ``status=proposed``); the PM decides via ROADMAP_TRACE_LINK."""
        proposed = [TraceLink.model_validate({**dict(link), "status": "proposed", "created_by": "agent"})
                    for link in links]
        with self._lock:
            for link in proposed:
                self._links[link.link_id] = link
        return [link.link_id for link in proposed]

    def run_named_query(self, name: str, params: Mapping[str, Any]) -> list[dict[str, Any]]:
        query = NAMED_QUERIES.get(name)
        if query is None:
            raise LookupError(f"unknown named query {name!r} (known: {sorted(NAMED_QUERIES)})")
        return query(self.evidence, params)


class AlloyDbStoreService:
    """Production ``mcp-store`` adapter (AlloyDB AI); blocked on SLB projects/instances (Q-10)."""

    def __init__(self, instance: str) -> None:
        self.instance = instance

    def _blocked(self, operation: str) -> NoReturn:
        not_implemented("Q-10", f"AlloyDB evidence store {operation} (instance {self.instance!r})")

    def put_signals(self, signals: Sequence[Signal]) -> dict[str, list[str]]:
        del signals
        self._blocked("put_signals")

    def evidence_search(self, query: str, top_k: int = 8) -> list[dict[str, Any]]:
        del query, top_k
        self._blocked("hybrid evidence_search")

    def save_draft(self, subject_type: str, draft: Mapping[str, Any]) -> dict[str, str]:
        del subject_type, draft
        self._blocked("save_draft")

    def trace_query(self, *, from_id: str | None = None, to_id: str | None = None, link_type: str | None = None,
                    status: str | None = None) -> list[dict[str, Any]]:
        del from_id, to_id, link_type, status
        self._blocked("trace_query")

    def trace_propose_links(self, links: Sequence[Mapping[str, Any]]) -> list[str]:
        del links
        self._blocked("trace_propose_links")

    def run_named_query(self, name: str, params: Mapping[str, Any]) -> list[dict[str, Any]]:
        del name, params
        self._blocked("run_named_query")
