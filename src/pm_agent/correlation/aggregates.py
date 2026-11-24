"""Aggregate claims and their V6 recomputation (docs/06 §3 V6, docs/05 §4.2 ``aggregate``).

Aggregates are produced **by code**, never by a model, from fixed templates; each counted item carries its own
citation. ``AggregateRecomputer`` re-derives the numbers from the cited evidence and fails closed on any template it
does not know.
"""

from __future__ import annotations

import re
from collections.abc import Sequence

from pm_agent.grounding.atoms import AtomExtractor
from pm_agent.grounding.evidence_store import InMemoryEvidenceStore
from pm_agent.schemas import CitedQuote, Claim, ClaimDraft, ClaimKind, Signal

CUSTOMERS = re.compile(r"^Reported by (\d+) customers?: (.+)\.$")
FREQUENCY = re.compile(r"^(\d+) linked signals? from (\d+) source systems?\.$")


def first_sentence(text: str) -> str:
    match = re.match(r"\s*([^.!?\n]{8,}[.!?]?)", text)
    return (match.group(1) if match else text[:120]).strip()[:300]


class AggregateBuilder:
    def __init__(self, store: InMemoryEvidenceStore, atoms: AtomExtractor) -> None:
        self._store = store
        self._atoms = atoms

    def _mention(self, signals: Sequence[Signal], canonical: str) -> CitedQuote | None:
        """First citation whose quote is a surface form (alias) of *canonical* inside a linked chunk."""
        forms = sorted((f for f, c in self._atoms.aliases.items() if c == canonical), key=len, reverse=True)
        for signal in signals:
            for chunk in self._store.chunks_for(signal.signal_id):
                for form in forms:
                    match = re.search(rf"(?<![A-Za-z0-9_]){re.escape(form)}(?![A-Za-z0-9_])", chunk.text,
                                      re.IGNORECASE)
                    if match:
                        return CitedQuote(evidence_id=chunk.evidence_id, quote=match.group(0))
        return None

    def customers(self, signals: Sequence[Signal], organizations: Sequence[str]) -> ClaimDraft | None:
        """``Reported by N customers: A, B.`` with one citation per customer (None when no customer is named)."""
        mentions = {org: cite for org in sorted(organizations) if (cite := self._mention(signals, org.lower()))}
        if not mentions:
            return None
        text = (f"Reported by {len(mentions)} customer{'s' if len(mentions) != 1 else ''}: "
                f"{', '.join(mentions)}.")
        return ClaimDraft(text=text, kind=ClaimKind.AGGREGATE, citations=list(mentions.values()))

    def frequency(self, signals: Sequence[Signal]) -> ClaimDraft | None:
        """``N linked signals from M source systems.`` with one citation per signal."""
        cites = []
        for signal in signals:
            chunks = self._store.chunks_for(signal.signal_id)
            if chunks:
                cites.append(CitedQuote(evidence_id=chunks[0].evidence_id, quote=first_sentence(chunks[0].text)))
        if not cites:
            return None
        systems = {s.source_system for s in signals if self._store.chunks_for(s.signal_id)}
        text = (f"{len(cites)} linked signal{'s' if len(cites) != 1 else ''} from {len(systems)} source "
                f"system{'s' if len(systems) != 1 else ''}.")
        return ClaimDraft(text=text, kind=ClaimKind.AGGREGATE, citations=cites)


class AggregateRecomputer:
    """V6 checker: recompute an aggregate claim from the evidence it cites."""

    def __init__(self, store: InMemoryEvidenceStore, atoms: AtomExtractor) -> None:
        self._store = store
        self._atoms = atoms

    def __call__(self, claim: Claim) -> bool:
        signals = {c.signal_id for c in claim.citations}
        if match := FREQUENCY.match(claim.text):
            systems = {c.source_system for c in claim.citations}
            return int(match.group(1)) == len(signals) == len(claim.citations) and int(match.group(2)) == len(systems)
        if match := CUSTOMERS.match(claim.text):
            named = [n.strip().lower() for n in match.group(2).split(",")]
            cited = set()
            for citation in claim.citations:
                cited.update(self._atoms.extract(citation.quote))
            return int(match.group(1)) == len(named) == len(set(named)) and set(named) <= cited
        return False  # unknown aggregate template ⇒ fail closed
