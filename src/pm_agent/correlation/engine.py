"""Signal → Product Problem correlation (docs/06 §6.1–6.2, W2 problem builder).

Scoring, strongest first:
1. **Hard links** (score 1.0): a cross-reference in the text (ADO URL, ``PD #…``, ``CS…``) in either direction, the
   parent record, or a W1 near-duplicate twin.
2. **Similarity** from the configured scorer (``correlation.scoring``): embeddings in production, lexical +
   identifier overlap offline. Thresholds come from the scorer: ≥ assign ⇒ AUTO_LINK, ≥ floor ⇒ ADJUDICATE
   (provisional link, PM confirmation), else NEW problem.

``assign_batch`` processes parents before children and merges *new* clusters that a hard link joins (a connected
component is one problem). Joining two *existing* problems is never automatic: it is returned as a merge candidate
for the PM (``PROBLEM_MERGED`` is recorded only after confirmation).
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

from pm_agent.correlation.repository import ProblemRepository
from pm_agent.correlation.scoring import SimilarityScorer, cosine, term_vector
from pm_agent.grounding.evidence_store import InMemoryEvidenceStore
from pm_agent.schemas import LinkType, Signal, new_id
from pm_agent.settings import Settings

Route = Literal["AUTO_LINK", "ADJUDICATE", "NEW"]


def similarity(a: str, b: str) -> float:
    """Content-word cosine (kept for callers that only have text)."""
    return cosine(term_vector(a), term_vector(b))


@dataclass(frozen=True)
class LinkCandidate:
    problem_id: str
    score: float
    link_type: LinkType
    reason: str


@dataclass(frozen=True)
class LinkDecision:
    signal_id: str
    route: Route
    candidate: LinkCandidate | None = None
    also_hard_linked: tuple[str, ...] = ()


@dataclass
class BatchAssignment:
    decisions: list[dict[str, Any]] = field(default_factory=list)
    merge_candidates: list[dict[str, Any]] = field(default_factory=list)
    new_problem_ids: list[str] = field(default_factory=list)


class CorrelationEngine:
    def __init__(self, settings: Settings, store: InMemoryEvidenceStore, problems: ProblemRepository,
                 scorer: SimilarityScorer) -> None:
        del settings  # thresholds live on the scorer
        self._store = store
        self._problems = problems
        self._scorer = scorer

    def _problem_of(self, signal_id: str, pending: Mapping[str, str]) -> str | None:
        return pending.get(signal_id) or self._problems.problem_for_signal(signal_id)

    def hard_links(self, signal: Signal, pending: Mapping[str, str],
                   twins: Mapping[str, str] | None = None) -> list[LinkCandidate]:
        """Deterministic links: cross-references (both directions), parent record, near-duplicate twin."""
        found: dict[str, LinkCandidate] = {}

        def add(signal_id: str | None, reason: str) -> None:
            problem_id = self._problem_of(signal_id, pending) if signal_id else None
            if problem_id and problem_id not in found:
                found[problem_id] = LinkCandidate(problem_id, 1.0, LinkType.HARD_REF, reason)

        for ref in signal.cross_refs:
            for other in self._store.find_signals(ref.system.value, ref.record_id):
                add(other.signal_id, f"{signal.source_system.value} text references {ref.system.value} "
                                     f"{ref.record_id}")
        add(signal.parent_signal_id, "child record of a linked signal")
        for other in self._store.signals.values():
            if any(r.system is signal.source_system and r.record_id == signal.source_record_id
                   for r in other.cross_refs):
                add(other.signal_id, f"{other.source_system.value} record references this record")
        if twins and signal.signal_id in twins:
            add(twins[signal.signal_id], "near-duplicate of a linked signal")
        return sorted(found.values(), key=lambda c: c.problem_id)

    def semantic(self, signal: Signal, pending: Mapping[str, str]) -> list[LinkCandidate]:
        """Best scorer result per problem over its linked signals."""
        by_problem: dict[str, list[str]] = {}
        for problem in self._problems.list_problems():
            by_problem[problem.problem_id] = [link.signal_id for link in problem.linked_signals]
        for signal_id, problem_id in pending.items():
            by_problem.setdefault(problem_id, []).append(signal_id)
        candidates = []
        for problem_id, signal_ids in by_problem.items():
            best, why = 0.0, ""
            for other_id in signal_ids:
                other = self._store.get_signal(other_id)
                if other is None or other.signal_id == signal.signal_id:
                    continue
                score, reason = self._scorer.score(signal, other)
                if score > best:
                    best, why = score, reason
            if best > 0:
                candidates.append(LinkCandidate(problem_id, best, LinkType.SEMANTIC, why))
        return sorted(candidates, key=lambda c: (-c.score, c.problem_id))

    def decide(self, signal: Signal, pending: Mapping[str, str] | None = None,
               twins: Mapping[str, str] | None = None) -> LinkDecision:
        """Route one signal: hard link ⇒ AUTO_LINK (other hard-linked problems reported); else by thresholds."""
        pending = pending or {}
        hard = self.hard_links(signal, pending, twins)
        if hard:
            return LinkDecision(signal.signal_id, "AUTO_LINK", hard[0], tuple(c.problem_id for c in hard[1:]))
        semantic = self.semantic(signal, pending)
        best = semantic[0] if semantic else None
        if best and best.score >= self._scorer.assign_threshold:
            return LinkDecision(signal.signal_id, "AUTO_LINK", best)
        if best and best.score >= self._scorer.adjudicate_floor:
            return LinkDecision(signal.signal_id, "ADJUDICATE", best)
        return LinkDecision(signal.signal_id, "NEW")

    def assign_batch(self, signals: Sequence[Signal], twins: Mapping[str, str] | None = None,
                     make_id: Callable[[str], str] = new_id) -> BatchAssignment:
        """Assign unlinked signals: parents first, then children; merge new clusters joined by a hard link."""
        pending: dict[str, str] = {}
        new_ids: list[str] = []
        result = BatchAssignment()
        ordered = sorted(signals, key=lambda s: (s.parent_signal_id is not None, s.captured_utc, s.signal_id))
        for signal in ordered:
            if self._problems.problem_for_signal(signal.signal_id):
                continue
            decision = self.decide(signal, pending, twins)
            candidate = decision.candidate
            if candidate is None:
                problem_id = make_id("prb")
                new_ids.append(problem_id)
            else:
                problem_id = self._merge(candidate.problem_id, decision.also_hard_linked, pending, new_ids,
                                         signal, result)
            pending[signal.signal_id] = problem_id
            result.decisions.append({"signal_id": signal.signal_id, "route": decision.route,
                                     "score": candidate.score if candidate else 1.0,
                                     "link_type": (candidate.link_type if candidate else LinkType.HARD_REF).value,
                                     "reason": candidate.reason if candidate else "first signal of a new problem"})
        for entry in result.decisions:
            entry["problem_id"] = pending[entry["signal_id"]]
        result.new_problem_ids = [pid for pid in new_ids if pid in pending.values()]
        return result

    def _merge(self, primary: str, others: tuple[str, ...], pending: dict[str, str], new_ids: list[str],
               signal: Signal, result: BatchAssignment) -> str:
        """Union of hard-linked clusters: new clusters fold into the oldest one; existing problems need the PM."""
        if not others:
            return primary
        involved = [primary, *others]
        existing = [pid for pid in involved if pid not in new_ids]
        canonical = existing[0] if existing else min(involved, key=new_ids.index)
        for problem_id in involved:
            if problem_id == canonical:
                continue
            if problem_id in new_ids:
                for signal_id, assigned in pending.items():
                    if assigned == problem_id:
                        pending[signal_id] = canonical
            else:
                result.merge_candidates.append({"problems": [canonical, problem_id], "signal_id": signal.signal_id,
                                                "reason": "hard links span existing problems"})
        return canonical
