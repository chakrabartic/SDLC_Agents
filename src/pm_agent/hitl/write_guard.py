"""Server-side approval-token enforcement for write MCP servers (docs/07 §6, docs/09 §1).

Every write server verifies the token **before** any outbound call — independently of the commit executor and of
Agent Gateway IAM (defense in depth). A token may back any number of ``validate_only`` pre-flight calls but exactly
one real execution (``jti`` registry; production: a ``hitl.token_executions`` row or Memorystore ``SET NX``).
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Mapping
from datetime import datetime
from typing import Any

from pm_agent.hitl.tokens import TokenError, verify_token
from pm_agent.schemas import ApprovalTokenClaims, utcnow


class WriteToolGuard:
    def __init__(self, *, keys: Mapping[str, bytes], audience: str, issuer: str, leeway_seconds: int = 30,
                 clock: Callable[[], datetime] = utcnow) -> None:
        self._keys = dict(keys)
        self._audience = audience
        self._issuer = issuer
        self._leeway = leeway_seconds
        self._clock = clock
        self._executed: set[str] = set()
        self._lock = threading.Lock()

    def authorize(self, *, scope: str, payload: Any, token: str | None, action_id: str | None,
                  validate_only: bool) -> ApprovalTokenClaims:
        """Verify the token for this exact tool + payload; burn its ``jti`` on the first real execution."""
        if not token:
            raise TokenError("missing_token", "write tools require an approval token")
        claims = verify_token(token, keys=self._keys, audience=self._audience, issuer=self._issuer, scope=scope,
                              payload=payload, action_id=action_id, now=self._clock().timestamp(),
                              leeway=self._leeway)
        if not validate_only:
            with self._lock:
                if claims.jti in self._executed:
                    raise TokenError("replayed", "token already used for an execution")
                self._executed.add(claims.jti)
        return claims
