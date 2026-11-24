"""svc-prototype-renderer: token-gated MCP release tool and plugin-facing program download."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from pm_agent.hitl.subjects import InMemorySubjectStore
from pm_agent.renderer.app import create_app
from pm_agent.renderer.backend import RendererBackend

pytestmark = pytest.mark.component


def test_program_download_requires_a_configured_plugin_token() -> None:
    backend = RendererBackend(InMemorySubjectStore())
    backend.programs["prg_1"] = "figma.createPage();\n"
    placeholder_app = TestClient(create_app(backend=backend))
    assert placeholder_app.get("/programs/prg_1", headers={"x-plugin-token": "anything"}).status_code == 401
    client = TestClient(create_app(backend=backend, plugin_token="s3cret-test-token"))
    assert client.get("/programs/prg_1", headers={"x-plugin-token": "wrong"}).status_code == 401
    assert client.get("/programs/prg_1", headers={"x-plugin-token": "s3cret-test-token"}).text.startswith("figma.")
    assert client.get("/programs/nope", headers={"x-plugin-token": "s3cret-test-token"}).status_code == 404


def test_local_renderer_serves_the_token_gated_mcp_tool() -> None:
    with TestClient(create_app()) as client:
        assert client.get("/healthz").json()["status"] == "ok"
        response = client.post("/mcp", headers={"accept": "application/json, text/event-stream"}, json={
            "jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}})
    assert response.status_code == 200
    assert [t["name"] for t in response.json()["result"]["tools"]] == ["release_prototype_spec"]
