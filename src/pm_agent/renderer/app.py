"""``svc-prototype-renderer`` HTTP app: program download for the private Figma plugin (+ health).

The ``release_prototype_spec`` MCP tool is mounted by ``pm_agent.serve`` through the MCP server factory; this module
only adds the plugin-facing download route. Plugin authentication is a placeholder until the Figma org plugin and
its auth model are confirmed (Q-02).
"""

from __future__ import annotations

import hmac

from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import PlainTextResponse

from pm_agent.placeholders import is_placeholder, placeholder
from pm_agent.renderer.backend import RendererBackend


def create_app(backend: RendererBackend, plugin_token: str = placeholder("Q-02", "figma_plugin_token")) -> FastAPI:
    """Health + token-gated program download (refuses every request while the plugin token is a placeholder)."""
    app = FastAPI(title="svc-prototype-renderer")

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/programs/{program_id}", response_class=PlainTextResponse)
    def program(program_id: str, x_plugin_token: str = Header(default="")) -> str:
        if is_placeholder(plugin_token) or not hmac.compare_digest(x_plugin_token, plugin_token):
            raise HTTPException(status_code=401, detail="plugin not authorized")
        source = backend.programs.get(program_id)
        if source is None:
            raise HTTPException(status_code=404, detail="unknown program")
        return source

    return app
