"""Product Problem store (docs/10 §3). Production: AlloyDB ``problems.*`` behind ``mcp-store``.

``ProblemStore`` is the port; ``ProblemRepository`` the in-memory reference implementation.
"""

from __future__ import annotations

import threading
from typing import Protocol

from pm_agent.schemas import LinkType, ProductProblem, SignalLink


class ProblemStore(Protocol):
    def get(self, problem_id: str) -> ProductProblem: ...

    def list_problems(self) -> list[ProductProblem]: ...

    def save(self, problem: ProductProblem) -> ProductProblem: ...

    def problem_for_signal(self, signal_id: str) -> str | None: ...

    def signal_ids(self, problem_id: str) -> list[str]: ...

    def link(self, problem_id: str, signal_id: str, link_type: LinkType, score: float, linked_by: str) -> None: ...


class ProblemRepository:
    """In-memory reference store; every write bumps ``version`` (optimistic concurrency in production).

    Values are copied on the way in and out, so callers can never mutate stored state behind the version counter.
    """

    def __init__(self) -> None:
        self._problems: dict[str, ProductProblem] = {}
        self._by_signal: dict[str, str] = {}
        self._lock = threading.RLock()

    def get(self, problem_id: str) -> ProductProblem:
        with self._lock:
            try:
                return self._problems[problem_id].model_copy(deep=True)
            except KeyError:
                raise LookupError(f"unknown problem {problem_id}") from None

    def list_problems(self) -> list[ProductProblem]:
        with self._lock:
            return [p.model_copy(deep=True) for p in self._problems.values()]

    def save(self, problem: ProductProblem) -> ProductProblem:
        """Insert or replace (version + 1 on replace) and index its signal links."""
        with self._lock:
            current = self._problems.get(problem.problem_id)
            stored = problem.model_copy(deep=True, update={} if current is None else {"version": current.version + 1})
            self._problems[problem.problem_id] = stored
            for link in stored.linked_signals:
                self._by_signal[link.signal_id] = stored.problem_id
            return stored.model_copy(deep=True)

    def problem_for_signal(self, signal_id: str) -> str | None:
        with self._lock:
            return self._by_signal.get(signal_id)

    def signal_ids(self, problem_id: str) -> list[str]:
        with self._lock:
            problem = self._problems.get(problem_id)
            return [link.signal_id for link in problem.linked_signals] if problem else []

    def link(self, problem_id: str, signal_id: str, link_type: LinkType, score: float, linked_by: str) -> None:
        """Attach a signal to an existing problem (idempotent)."""
        with self._lock:
            problem = self._problems[problem_id]
            if any(link.signal_id == signal_id for link in problem.linked_signals):
                return
            links = [*problem.linked_signals, SignalLink(signal_id=signal_id, link_type=link_type,
                                                         score=round(min(max(score, 0.0), 1.0), 4),
                                                         linked_by=linked_by)]
            self.save(problem.model_copy(update={"linked_signals": links}))
