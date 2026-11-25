"""Server-side approval-token enforcement for write MCP servers (docs/07 §6, docs/09 §1).

Every write server verifies the token **before** any outbound call — independently of the commit executor and of
Agent Gateway IAM (defense in depth). A token may back any number of ``validate_only`` pre-flight calls but exactly
one real execution, enforced by a ``JtiRegistry`` (in-process with expiry pruning for local/dev; Memorystore
``SET NX EX`` or AlloyDB ``hitl.token_executions`` across Cloud Run instances in stage/prod).
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Mapping
from datetime import datetime
from typing import Any, Protocol

from pm_agent.hitl.tokens import TokenError, verify_token
from pm_agent.schemas import ApprovalTokenClaims, utcnow


class JtiRegistry(Protocol):
    """Single-use ``jti`` claim store (production: Memorystore ``SET NX EX`` or AlloyDB ``INSERT … ON CONFLICT``)."""

    def claim(self, jti: str, expires_at: float) -> bool:
        """Atomically claim *jti* until *expires_at* (epoch seconds). ``True`` once, ``False`` on any replay."""


class InMemoryJtiRegistry:
    """Thread-safe in-process ``JtiRegistry`` for local/dev and tests; prunes entries past their expiry."""

    def __init__(self, clock: Callable[[], datetime] = utcnow) -> None:
        self._clock = clock
        self._claimed: dict[str, float] = {}
        self._lock = threading.Lock()

    def claim(self, jti: str, expires_at: float) -> bool:
        """Prune expired entries, then claim *jti* once until *expires_at* (epoch seconds)."""
        now = self._clock().timestamp()
        with self._lock:
            expired = [key for key, exp in self._claimed.items() if exp <= now]
            for key in expired:
                del self._claimed[key]
            if jti in self._claimed:
                return False
            self._claimed[jti] = expires_at
            return True

    def __len__(self) -> int:
        with self._lock:
            return len(self._claimed)


class WriteToolGuard:
    def __init__(self, *, keys: Mapping[str, bytes], audience: str, issuer: str, leeway_seconds: int = 30,
                 clock: Callable[[], datetime] = utcnow, registry: JtiRegistry | None = None) -> None:
        self._keys = dict(keys)
        self._audience = audience
        self._issuer = issuer
        self._leeway = leeway_seconds
        self._clock = clock
        self._registry: JtiRegistry = registry if registry is not None else InMemoryJtiRegistry(clock=clock)

    def authorize(self, *, scope: str, payload: Any, token: str | None, action_id: str | None,
                  validate_only: bool) -> ApprovalTokenClaims:
        """Verify the token for this exact tool + payload; burn its ``jti`` on the first real execution."""
        if not token:
            raise TokenError("missing_token", "write tools require an approval token")
        claims = verify_token(token, keys=self._keys, audience=self._audience, issuer=self._issuer, scope=scope,
                              payload=payload, action_id=action_id, now=self._clock().timestamp(),
                              leeway=self._leeway)
        if not validate_only and not self._registry.claim(claims.jti, claims.exp + self._leeway):
            raise TokenError("replayed", "token already used for an execution")
        return claims
