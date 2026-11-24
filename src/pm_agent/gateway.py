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

from pm_agent.placeholders import not_implemented


class ToolCallError(Exception):
    """Typed tool failure (docs/09 §1): unauthorized, not_found, validation_failed, unavailable, rate_limited."""

    def __init__(self, code: str, message: str = "") -> None:
        super().__init__(f"{code}: {message}" if message else code)
        self.code = code


class ToolServer(Protocol):
    name: str

    def call(self, tool: str, arguments: dict[str, Any]) -> dict[str, Any]: ...


class ToolGateway(Protocol):
    def call(self, server: str, tool: str, arguments: dict[str, Any]) -> dict[str, Any]: ...


class InProcessToolGateway:
    def __init__(self, servers: Mapping[str, ToolServer]) -> None:
        self._servers = dict(servers)

    def call(self, server: str, tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
        target = self._servers.get(server)
        if target is None:
            raise ToolCallError("not_found", f"no route to server {server}")
        return target.call(tool, arguments)


class McpGatewayClient:
    """Production gateway: MCP over Streamable HTTP via Agent Gateway egress (endpoint per Agent Registry entry)."""

    def __init__(self, endpoints: Mapping[str, str]) -> None:
        self._endpoints = dict(endpoints)

    def call(self, server: str, tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
        del tool, arguments
        if server not in self._endpoints:
            raise ToolCallError("not_found", f"no Agent Registry endpoint for {server}")
        not_implemented("Q-10", "MCP client call through Agent Gateway egress (registry endpoint per environment)")
