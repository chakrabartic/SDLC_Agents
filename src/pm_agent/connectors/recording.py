"""Recording write backends for local runs and tests — they never leave the process.

They stand in for the vendor clients behind write servers until SLB sandboxes exist (Q-01, Q-04, Q-13, Q-14) and
behave like well-mannered vendors: ``validate_only`` validates, real calls are idempotent per ``idempotency_key``.
"""

from __future__ import annotations

import threading
from typing import Any

from pm_agent.gateway import WriteHandler


class RecordingWriteBackend:
    def __init__(self, system: str) -> None:
        self.system = system
        self.calls: list[dict[str, Any]] = []
        self._created: dict[str, dict[str, Any]] = {}
        self._lock = threading.Lock()

    def handler(self, tool: str) -> WriteHandler:
        """A handler for *tool* that records the call and returns a synthetic external reference."""

        def _handle(*, payload: Any, target: dict[str, Any], validate_only: bool,
                    idempotency_key: str) -> dict[str, Any]:
            with self._lock:
                self.calls.append({"tool": tool, "payload": payload, "target": target,
                                   "validate_only": validate_only, "idempotency_key": idempotency_key})
                if validate_only:
                    return {"status": "valid"}
                if idempotency_key in self._created:  # retry after a lost response: same object, no duplicate
                    return self._created[idempotency_key]
                external_id = str(4000 + len(self._created) + 1)
                result = {"status": "created", "external_id": external_id,
                          "url": f"https://{self.system}.example.invalid/{tool}/{external_id}", "echo": payload}
                self._created[idempotency_key] = result
                return result

        return _handle

    def preflights(self) -> list[dict[str, Any]]:
        return [c for c in self.calls if c["validate_only"]]

    def creates(self) -> list[dict[str, Any]]:
        return [c for c in self.calls if not c["validate_only"]]
