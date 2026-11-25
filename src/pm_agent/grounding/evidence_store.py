"""Evidence access used by the Verifier and agents (docs/10 §7).

``EvidenceStore`` is the interface; ``InMemoryEvidenceStore`` is the reference implementation used locally and in
tests. The production adapter (AlloyDB AI: pgvector + ScaNN + tsvector) implements the same protocol behind
``mcp-store`` (milestone M1).
"""

from __future__ import annotations

import re
from typing import Protocol

from pm_agent.schemas import EvidenceChunk, Signal, SignalStatus

_WORD = re.compile(r"[a-z0-9_]+")


class EvidenceStore(Protocol):
    """Read access the Verifier needs (V2 resolution)."""

    def get_chunk(self, evidence_id: str) -> EvidenceChunk | None: ...

    def get_signal(self, signal_id: str) -> Signal | None: ...


class EvidenceRepository(EvidenceStore, Protocol):
    """The full evidence port used by ingestion, workflows and correlation (AlloyDB adapter in production)."""

    def add_signal(self, signal: Signal) -> None: ...

    def add_chunk(self, chunk: EvidenceChunk) -> None: ...

    def set_signal_status(self, signal_id: str, status: SignalStatus) -> None: ...

    def list_signals(self, *, active_only: bool = False) -> list[Signal]: ...

    def chunks_for(self, signal_id: str) -> list[EvidenceChunk]: ...

    def find_signals(self, source_system: str, source_record_id: str) -> list[Signal]: ...

    def search(self, query: str, top_k: int = 8) -> list[EvidenceChunk]: ...


class InMemoryEvidenceStore:
    """Dictionary-backed store with naive lexical search (reference implementation)."""

    def __init__(self) -> None:
        self.signals: dict[str, Signal] = {}
        self.chunks: dict[str, EvidenceChunk] = {}

    def add_signal(self, signal: Signal) -> None:
        self.signals[signal.signal_id] = signal

    def add_chunk(self, chunk: EvidenceChunk) -> None:
        if chunk.signal_id not in self.signals:
            raise KeyError(f"unknown signal {chunk.signal_id}")
        self.chunks[chunk.evidence_id] = chunk

    def get_chunk(self, evidence_id: str) -> EvidenceChunk | None:
        return self.chunks.get(evidence_id)

    def get_signal(self, signal_id: str) -> Signal | None:
        return self.signals.get(signal_id)

    def set_signal_status(self, signal_id: str, status: SignalStatus) -> None:
        self.signals[signal_id] = self.signals[signal_id].model_copy(update={"status": status})

    def list_signals(self, *, active_only: bool = False) -> list[Signal]:
        """Every stored signal in insertion order (only ``ACTIVE`` ones with *active_only*)."""
        return [s for s in self.signals.values() if not active_only or s.status is SignalStatus.ACTIVE]

    def chunks_for(self, signal_id: str) -> list[EvidenceChunk]:
        return sorted((c for c in self.chunks.values() if c.signal_id == signal_id), key=lambda c: c.seq)

    def find_signals(self, source_system: str, source_record_id: str) -> list[Signal]:
        """Active signals for one source record (any version) — resolves cross-reference hard links."""
        return [s for s in self.signals.values() if s.source_system.value == source_system
                and s.source_record_id == source_record_id and s.status is SignalStatus.ACTIVE]

    def search(self, query: str, top_k: int = 8) -> list[EvidenceChunk]:
        """Lexical overlap ranking over active signals (stand-in for hybrid vector + tsvector search)."""
        terms = set(_WORD.findall(query.lower()))
        scored: list[tuple[int, str, EvidenceChunk]] = []
        for chunk in self.chunks.values():
            signal = self.signals.get(chunk.signal_id)
            if signal is None or signal.status is not SignalStatus.ACTIVE:
                continue
            overlap = len(terms & set(_WORD.findall(chunk.text.lower())))
            if overlap:
                scored.append((overlap, chunk.evidence_id, chunk))
        scored.sort(key=lambda item: (-item[0], item[1]))
        return [chunk for _, _, chunk in scored[:top_k]]
