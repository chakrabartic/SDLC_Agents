"""``mcp-correlation`` re-clustering job (``PM_AGENT_JOB=recluster``, docs/06 §6.2).

Re-scores provisional (grey-zone, ``semantic``) links against the current problem set and **proposes** changes —
it never merges or moves signals itself (low-confidence merges require PM confirmation, requirements §13).
Production adds HDBSCAN over AlloyDB embeddings; the proposal contract stays the same.
"""

from __future__ import annotations

from typing import Any

from pm_agent.correlation import CorrelationEngine, ProblemRepository
from pm_agent.correlation.scoring import SimilarityScorer, make_scorer
from pm_agent.grounding.atoms import AtomExtractor
from pm_agent.grounding.evidence_store import InMemoryEvidenceStore
from pm_agent.jobs import emit
from pm_agent.placeholders import not_implemented
from pm_agent.schemas import LinkType
from pm_agent.settings import Settings, load_settings


def proposals(settings: Settings, store: InMemoryEvidenceStore, problems: ProblemRepository, *,
              scorer: SimilarityScorer | None = None) -> list[dict[str, Any]]:
    """For every provisional link, the problem the signal now fits best (only when it differs).

    Uses the configured scorer (``make_scorer``) and *that* scorer's ``assign_threshold`` so embedding and lexical
    scores are never compared against the wrong cut-off.
    """
    active_scorer = scorer if scorer is not None else make_scorer(settings, AtomExtractor.from_settings(settings))
    engine = CorrelationEngine(settings, store, problems, active_scorer)
    found = []
    for problem in problems.list_problems():
        for link in problem.linked_signals:
            if link.link_type is not LinkType.SEMANTIC or link.score >= active_scorer.assign_threshold:
                continue
            signal = store.get_signal(link.signal_id)
            if signal is None:
                continue
            others = [c for c in engine.semantic(signal, {}) if c.problem_id != problem.problem_id]
            if others and others[0].score > link.score:
                found.append({"signal_id": signal.signal_id, "from_problem": problem.problem_id,
                              "to_problem": others[0].problem_id, "score": others[0].score,
                              "current_score": link.score, "action": "propose_move_for_pm_confirmation"})
    return found


def main() -> int:
    settings = load_settings()
    if settings.strict:
        not_implemented("Q-10", "AlloyDB-backed evidence and problem stores for re-clustering")
    found = proposals(settings, InMemoryEvidenceStore(), ProblemRepository())
    emit("INFO", job="recluster", proposals=len(found))
    return 0
