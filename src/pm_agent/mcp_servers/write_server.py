"""Write MCP servers' core: token check first, then the single vendor call (docs/09 §1, docs/07 §6).

``WriteToolServer`` is transport-independent; ``pm_agent.mcp_servers.factory`` exposes it over MCP Streamable HTTP
with truthful annotations (``readOnlyHint=false``). Handlers receive only what the PM approved.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from pm_agent.gateway import ToolCallError, WriteHandler
from pm_agent.hitl.catalog import scope_for
from pm_agent.hitl.tokens import TokenError
from pm_agent.hitl.write_guard import WriteToolGuard

__all__ = ["WriteHandler", "WriteToolServer"]


class WriteToolServer:
    def __init__(self, name: str, guard: WriteToolGuard, handlers: Mapping[str, WriteHandler]) -> None:
        self.name = name
        self._guard = guard
        self._handlers = dict(handlers)

    @property
    def tools(self) -> list[str]:
        return sorted(self._handlers)

    def call(self, tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """Authorize (token ⇒ exact tool + payload + action), then run the handler once."""
        handler = self._handlers.get(tool)
        if handler is None:
            raise ToolCallError("not_found", f"{self.name} has no tool {tool}")
        payload = arguments.get("payload")
        validate_only = bool(arguments.get("validate_only", False))
        action_id = arguments.get("action_id")
        if not validate_only and not (isinstance(action_id, str) and action_id):
            raise ToolCallError("unauthorized", "missing action_id")
        try:
            claims = self._guard.authorize(scope=scope_for(self.name, tool), payload=payload,
                                           token=arguments.get("approval_token"), action_id=action_id,
                                           validate_only=validate_only)
        except TokenError as exc:
            raise ToolCallError("unauthorized", str(exc)) from exc
        return handler(payload=payload, target=claims.tgt, validate_only=validate_only, idempotency_key=claims.act)
