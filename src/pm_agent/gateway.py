"""Tool gateway abstraction for zero-LLM callers (commit executor, ingestion workflow).

LLM agents reach MCP servers through ADK ``McpToolset`` behind Agent Gateway. Function nodes need the same tools
without a model in the loop: ``ToolGateway.call(server, tool, arguments)``.

* ``InProcessToolGateway`` — local/dev/tests: dispatches to in-process server objects.
* Production: an MCP Streamable-HTTP client that calls the server's Agent Registry endpoint through Agent Gateway
  egress with the caller's Agent Identity (see ``McpGatewayClient`` placeholder; endpoint ids are Terraform outputs).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Protocol

from pydantic import ValidationError

from pm_agent.placeholders import PlaceholderError, PlaceholderNotImplemented, not_implemented


class ToolCallError(Exception):
    """Typed tool failure (docs/09 §1): unauthorized, not_found, validation_failed, unavailable, rate_limited."""

    def __init__(self, code: str, message: str = "") -> None:
        super().__init__(f"{code}: {message}" if message else code)
        self.code = code


class ToolServer(Protocol):
    name: str

    def call(self, tool: str, arguments: dict[str, Any]) -> dict[str, Any]: ...


class WriteHandler(Protocol):
    """One vendor write behind a token-gated write server (docs/07 §6, docs/09 §1).

    Called only after the server verified the approval token for this exact tool, payload and action; ``target`` is
    the approved ``tgt`` claim and ``idempotency_key`` the action id. Keyword-only so every handler is type-checked.
    """

    def __call__(self, *, payload: Any, target: dict[str, Any], validate_only: bool,
                 idempotency_key: str) -> dict[str, Any]: ...


class ToolGateway(Protocol):
    def call(self, server: str, tool: str, arguments: dict[str, Any]) -> dict[str, Any]: ...


def _translate_server_error(exc: Exception) -> ToolCallError | None:
    """Translate anticipated in-process server errors into typed ``ToolCallError`` (never echo content).

    Mirrors ``pm_agent.mcp_servers.factory.typed_errors`` without importing connectors or HITL modules (keeps
    ``gateway`` cycle-free below every server and connector package). Unanticipated errors return ``None`` and escape.
    """
    if isinstance(exc, PlaceholderError):
        detail = f"awaiting {exc.question_id}" if isinstance(exc, PlaceholderNotImplemented) else type(exc).__name__
        return ToolCallError("unavailable", detail)
    if isinstance(exc, LookupError):
        return ToolCallError("not_found", type(exc).__name__)
    if isinstance(exc, (ValidationError, ValueError, TypeError)):
        return ToolCallError("validation_failed", type(exc).__name__)
    code = getattr(exc, "code", None)
    if isinstance(code, str) and type(exc).__module__.startswith("pm_agent."):
        return ToolCallError(code, type(exc).__name__)
    return None


class InProcessToolGateway:
    def __init__(self, servers: Mapping[str, ToolServer]) -> None:
        self._servers = dict(servers)

    def call(self, server: str, tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """Dispatch to an in-process server and translate anticipated errors into typed ``ToolCallError``s."""
        target = self._servers.get(server)
        if target is None:
            raise ToolCallError("not_found", f"no route to server {server}")
        try:
            return target.call(tool, arguments)
        except ToolCallError:
            raise
        except Exception as exc:  # pylint: disable=broad-exception-caught  # translate anticipated server errors only
            translated = _translate_server_error(exc)
            if translated is not None:
                raise translated from exc
            raise


class McpGatewayClient:
    """Production gateway: MCP over Streamable HTTP via Agent Gateway egress (endpoint per Agent Registry entry)."""

    def __init__(self, endpoints: Mapping[str, str]) -> None:
        self._endpoints = dict(endpoints)

    def call(self, server: str, tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
        del tool, arguments
        if server not in self._endpoints:
            raise ToolCallError("not_found", f"no Agent Registry endpoint for {server}")
        not_implemented("Q-10", "MCP client call through Agent Gateway egress (registry endpoint per environment)")
