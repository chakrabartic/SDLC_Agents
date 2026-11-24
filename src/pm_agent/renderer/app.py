"""``svc-prototype-renderer`` HTTP app: the ``release_prototype_spec`` MCP tool + program download for the plugin.

* ``POST /mcp`` — the token-gated write tool the commit executor calls (MCP Streamable HTTP via Agent Gateway).
* ``GET /programs/{id}`` — the compiled Plugin API program, fetched by the private SLB Figma plugin. Plugin
  authentication is a placeholder until the org plugin and its auth model are confirmed (Q-02); while the token is a
  placeholder every download is refused.
Local/dev wiring comes from ``runtime.build_services``; stage/prod need a spec-store client (``mcp-store``) and fail
loudly until it exists (Q-10).
"""

from __future__ import annotations

import hmac
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import PlainTextResponse
from starlette.applications import Starlette

from pm_agent.mcp_servers.write_server import WriteToolServer
from pm_agent.placeholders import is_placeholder, placeholder
from pm_agent.renderer.backend import RendererBackend
from pm_agent.settings import Settings

PLUGIN_TOKEN_ENV = "PM_AGENT_RENDERER_PLUGIN_TOKEN"
SERVER = "svc-prototype-renderer"


def create_app(settings: Settings | None = None, backend: RendererBackend | None = None,
               plugin_token: str | None = None) -> FastAPI:
    """Renderer service; with no *backend* it wires itself from the environment's composition root."""
    token = plugin_token or os.environ.get(PLUGIN_TOKEN_ENV) or placeholder("Q-02", "figma_plugin_token")
    write_server: WriteToolServer | None = None
    if backend is None:
        from pm_agent.runtime import build_services  # pylint: disable=import-outside-toplevel

        services = build_services(settings)
        found = services.write_backends.get(SERVER)
        if not isinstance(found, RendererBackend):
            raise RuntimeError("the renderer backend is not wired in this environment")
        backend, write_server = found, services.write_servers.get(SERVER)
        settings = services.settings
    mcp_app: Starlette | None = None
    if write_server is not None and settings is not None:
        from pm_agent.mcp_servers.factory import build_server, http_app  # pylint: disable=import-outside-toplevel
        from pm_agent.mcp_servers.services import ServerServices  # pylint: disable=import-outside-toplevel

        services_view = ServerServices(settings=settings, write_servers={SERVER: write_server})
        mcp_app = http_app(build_server(SERVER, services_view))

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        """Run the mounted MCP app's session manager (sub-app lifespans are not run by FastAPI)."""
        if mcp_app is None:
            yield
            return
        async with mcp_app.router.lifespan_context(mcp_app):
            yield

    app = FastAPI(title=SERVER, lifespan=lifespan)

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/programs/{program_id}", response_class=PlainTextResponse)
    def program(program_id: str, x_plugin_token: str = Header(default="")) -> str:
        if is_placeholder(token) or not hmac.compare_digest(x_plugin_token, token):
            raise HTTPException(status_code=401, detail="plugin not authorized")
        source = backend.programs.get(program_id)
        if source is None:
            raise HTTPException(status_code=404, detail="unknown program")
        return source

    if mcp_app is not None:
        app.mount("/", mcp_app)
    return app
