"""Per-source ingestion state: delta cursors and the last poll status (docs/09 §1, IR3).

Production persists this in ``mcp-store`` (AlloyDB). It is committed once per **completed** ingestion window —
after the window's signals are stored and linked to problems — so a failure anywhere in the window re-polls it
instead of silently skipping it, and the problem inbox reports the health of the last completed refresh.
"""

from __future__ import annotations

import threading
from collections.abc import Mapping
from typing import Protocol


class IngestionState(Protocol):
    def cursor(self, source: str) -> str | None: ...

    def commit_window(self, *, cursors: Mapping[str, str | None], health: Mapping[str, str]) -> None: ...

    def health(self) -> dict[str, str]: ...


class InMemoryIngestionState:
    """Reference implementation (local/dev, tests)."""

    def __init__(self) -> None:
        self._cursors: dict[str, str | None] = {}
        self._health: dict[str, str] = {}
        self._lock = threading.Lock()

    def cursor(self, source: str) -> str | None:
        with self._lock:
            return self._cursors.get(source)

    def commit_window(self, *, cursors: Mapping[str, str | None], health: Mapping[str, str]) -> None:
        """Advance the given cursors and record every source's status, atomically."""
        with self._lock:
            self._cursors.update(cursors)
            self._health.update(health)

    def health(self) -> dict[str, str]:
        with self._lock:
            return dict(sorted(self._health.items()))
