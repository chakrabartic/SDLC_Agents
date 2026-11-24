"""Evidence chunking (docs/10 §2.2): split a redacted ``Signal`` into citable, contiguous slices.

Each chunk is an exact slice of ``Signal.raw_text`` (V2 re-checks this), bounded in size, cut preferably at
paragraph, then sentence, then word boundaries. Evidence ids are derived from the signal id, its provenance hash and
the sequence number, so re-ingesting the same record version is idempotent.
"""

from __future__ import annotations

import re

from pm_agent.schemas import EvidenceChunk, Signal, sha256_hex

DEFAULT_MAX_CHARS = 1200
_BREAKS = (re.compile(r"\n\s*\n"), re.compile(r"(?<=[.!?;])\s+"), re.compile(r"\s+"))


def evidence_id_for(signal: Signal, seq: int) -> str:
    return "ev_" + sha256_hex(f"{signal.signal_id}|{signal.provenance_hash}|{seq}")[:26]


def _cut(text: str, start: int, limit: int) -> int:
    """Best cut position in ``text[start:limit]``: the last paragraph/sentence/word break past one third."""
    floor = start + max(1, (limit - start) // 3)
    for pattern in _BREAKS:
        best = None
        for match in pattern.finditer(text, floor, limit):
            best = match.end()
        if best is not None and best < limit:
            return best
    return limit


def segment(text: str, max_chars: int = DEFAULT_MAX_CHARS) -> list[tuple[int, int]]:
    """``(start, end)`` spans of non-blank chunks, each at most *max_chars* long and trimmed of outer whitespace."""
    if max_chars < 1:
        raise ValueError("max_chars must be positive")
    spans: list[tuple[int, int]] = []
    pos, size = 0, len(text)
    while pos < size:
        while pos < size and text[pos].isspace():
            pos += 1
        if pos >= size:
            break
        end = size if size - pos <= max_chars else _cut(text, pos, pos + max_chars)
        stop = end
        while stop > pos and text[stop - 1].isspace():
            stop -= 1
        spans.append((pos, stop))
        pos = end
    return spans


def chunk_signal(signal: Signal, max_chars: int = DEFAULT_MAX_CHARS) -> list[EvidenceChunk]:
    """Citable chunks of *signal* (the only units an agent may cite)."""
    text = signal.raw_text
    return [EvidenceChunk(evidence_id=evidence_id_for(signal, seq), signal_id=signal.signal_id, seq=seq,
                          text=text[start:end], char_start=start, char_end=end)
            for seq, (start, end) in enumerate(segment(text, max_chars))]
