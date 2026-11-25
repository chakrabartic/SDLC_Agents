"""``mcp-correlation`` backend over the correlation engine (docs/06 §6, docs/12 §2 ``correlation-engine``).

``EngineCorrelationService`` adapts ``pm_agent.correlation.CorrelationEngine`` (hard links first, then semantic
similarity with the configured thresholds) to the three MCP tools. Auto-links are written to the problem repository;
borderline signals are queued for adjudication (PM confirmation or the cited LLM adjudicator) — never linked silently.
The GKE deployment also passes ``PM_AGENT_SUBSCRIPTION`` (``signal-ingested`` Pub/Sub subscription); draining it is a
placeholder until the SLB projects exist (Q-10).
"""

from __future__ import annotations

import threading
from collections.abc import Sequence
from typing import Any, NoReturn

from pm_agent.correlation import CorrelationEngine, ProblemStore
from pm_agent.grounding.evidence_store import EvidenceRepository
from pm_agent.placeholders import not_implemented
from pm_agent.schemas import Signal

LINKED_BY = "mcp-correlation"


class EngineCorrelationService:
    def __init__(self, engine: CorrelationEngine, evidence: EvidenceRepository, problems: ProblemStore, *,
                 subscription: str | None = None) -> None:
        self._engine = engine
        self._evidence = evidence
        self._problems = problems
        self.subscription = subscription
        self.adjudication_queue: list[dict[str, Any]] = []
        self._lock = threading.Lock()

    def _signal(self, signal_id: str) -> Signal:
        signal = self._evidence.get_signal(signal_id)
        if signal is None:
            raise LookupError(f"unknown signal {signal_id}")
        return signal

    def find_candidates(self, signal_id: str, top_k: int = 5) -> list[dict[str, Any]]:
        """Candidate problems for one stored signal: hard links (score 1.0) then semantic similarity."""
        signal = self._signal(signal_id)
        best: dict[str, dict[str, Any]] = {}
        for candidate in [*self._engine.hard_links(signal, {}), *self._engine.semantic(signal, {})]:
            current = best.get(candidate.problem_id)
            if current is None or candidate.score > current["score"]:
                best[candidate.problem_id] = {"problem_id": candidate.problem_id, "score": candidate.score,
                                              "link_type": candidate.link_type.value, "reason": candidate.reason}
        return sorted(best.values(), key=lambda c: (-c["score"], c["problem_id"]))[:max(top_k, 0)]

    def assign_candidates(self, signal_ids: Sequence[str]) -> list[dict[str, Any]]:
        """Route each signal: AUTO_LINK is applied (idempotent), ADJUDICATE is queued, NEW is reported."""
        results = []
        with self._lock:
            for signal_id in signal_ids:
                decision = self._engine.decide(self._signal(signal_id))
                candidate = decision.candidate
                outcome: dict[str, Any] = {"signal_id": signal_id, "route": decision.route,
                                           "problem_id": candidate.problem_id if candidate else None,
                                           "score": candidate.score if candidate else None}
                if decision.route == "AUTO_LINK" and candidate is not None:
                    self._problems.link(candidate.problem_id, signal_id, candidate.link_type, candidate.score,
                                        linked_by=LINKED_BY)
                elif decision.route == "ADJUDICATE" and outcome not in self.adjudication_queue:
                    self.adjudication_queue.append(outcome)
                results.append(outcome)
        return results

    def related(self, signal_id: str) -> dict[str, Any]:
        """Deterministic neighbourhood of a signal: parent, children, cross-references both ways, its problem."""
        signal = self._signal(signal_id)
        signals = self._evidence.list_signals()
        referenced = [other.signal_id for ref in signal.cross_refs
                      for other in self._evidence.find_signals(ref.system.value, ref.record_id)]
        referencing = [other.signal_id for other in signals if other.signal_id != signal_id and any(
            r.system is signal.source_system and r.record_id == signal.source_record_id for r in other.cross_refs)]
        problem_id = self._problems.problem_for_signal(signal_id)
        return {"signal_id": signal_id, "parent": signal.parent_signal_id,
                "children": [other.signal_id for other in signals if other.parent_signal_id == signal_id],
                "cross_referenced": sorted(set(referenced)), "referencing": sorted(set(referencing)),
                "problem_id": problem_id,
                "problem_signals": self._problems.signal_ids(problem_id) if problem_id else []}

    def drain_subscription(self) -> NoReturn:
        """Pull ``signal-ingested`` events to keep the candidate index warm (production only)."""
        not_implemented("Q-10", f"Pub/Sub pull subscription {self.subscription!r} for signal-ingested events")
