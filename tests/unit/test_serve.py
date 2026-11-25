"""Container dispatcher (``python -m pm_agent.serve``): one image per component, chosen by ``PM_AGENT_COMPONENT``.

Uvicorn is never started here (a fake runner captures the call); jobs and the lead-owned ASGI apps are loaded through
``load_target`` which the tests replace — only the dispatch contract is under test.
"""
from __future__ import annotations

from typing import Any

import pytest
from starlette.testclient import TestClient

from pm_agent import serve
from pm_agent.placeholders import PlaceholderNotImplemented
from pm_agent.settings import Environment, Settings, load_settings

ALL_COMPONENTS = ["pm-workbench", "signal-event-gateway", "mcp-verifier", "mcp-ledger", "mcp-store", "mcp-correlation",
                  "mcp-ado", "mcp-ado-write", "mcp-servicenow", "mcp-pagerduty", "mcp-msgraph", "mcp-msgraph-write",
                  "mcp-figma", "mcp-figma-write", "mcp-kb", "mcp-kb-write", "svc-prototype-renderer",
                  "dls-catalog-sync", "ledger-anchor"]
MCP_HEADERS = {"accept": "application/json, text/event-stream", "content-type": "application/json",
               "mcp-protocol-version": "2025-06-18"}


@pytest.fixture(name="settings")
def _settings() -> Settings:
    return load_settings(environ={"PM_AGENT_ENV": "local"})


class FakeRunner:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def __call__(self, app: Any, **kwargs: Any) -> None:
        self.calls.append({"app": app, **kwargs})


def test_every_documented_component_resolves() -> None:
    assert sorted(serve.COMPONENTS) == sorted(ALL_COMPONENTS)
    kinds = {name: serve.resolve_component(name).kind for name in ALL_COMPONENTS}
    assert kinds["signal-event-gateway"] == "gateway"
    assert kinds["dls-catalog-sync"] == kinds["ledger-anchor"] == "job"
    assert kinds["pm-workbench"] == kinds["svc-prototype-renderer"] == "asgi"
    assert all(kind == "mcp" for name, kind in kinds.items() if name.startswith("mcp-"))
    assert serve.resolve_component("pm-workbench").target == "pm_agent.workbench.app:create_app"
    assert serve.resolve_component("svc-prototype-renderer").target == "pm_agent.runtime:renderer_app"
    assert serve.resolve_component("dls-catalog-sync").target == "pm_agent.jobs.dls_catalog_sync:main"
    assert serve.resolve_component("ledger-anchor").target == "pm_agent.jobs.ledger_anchor:main"


def test_unknown_or_missing_component_is_a_clear_error() -> None:
    with pytest.raises(serve.UnknownComponent) as excinfo:
        serve.resolve_component("mcp-nope")
    assert "mcp-nope" in str(excinfo.value) and "mcp-ado" in str(excinfo.value)
    with pytest.raises(serve.UnknownComponent, match="PM_AGENT_COMPONENT"):
        serve.resolve_component(None)
    assert serve.main(environ={"PM_AGENT_ENV": "local"}, runner=FakeRunner()) == 2


def test_jobs_are_resolved_from_pm_agent_job() -> None:
    job = serve.resolve_job("recluster")
    assert job.kind == "job" and job.target == "pm_agent.jobs.recluster:main"
    with pytest.raises(serve.UnknownComponent, match="recluster"):
        serve.resolve_job("vacuum")


def test_mcp_component_serves_mcp_and_healthz(settings: Settings) -> None:
    app = serve.build_app(serve.resolve_component("mcp-pagerduty"), settings, environ={})
    with TestClient(app) as client:
        assert client.get("/healthz").json() == {"status": "ok", "server": "mcp-pagerduty"}
        listed = client.post("/mcp", headers=MCP_HEADERS, json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    assert sorted(t["name"] for t in listed.json()["result"]["tools"]) == \
        ["list_incidents", "list_log_entries", "list_notes"]


def test_gateway_component_builds_the_webhook_app(settings: Settings) -> None:
    app = serve.build_app(serve.resolve_component("signal-event-gateway"), settings, environ={})
    with TestClient(app) as client:
        assert client.get("/healthz").json()["component"] == "signal-event-gateway"
        assert client.post("/webhooks/pagerduty", content=b"{}").status_code == 401  # secrets unconfigured


def test_main_starts_uvicorn_on_port(settings: Settings) -> None:
    del settings
    runner = FakeRunner()
    code = serve.main(environ={"PM_AGENT_ENV": "local", "PM_AGENT_COMPONENT": "mcp-kb", "PORT": "9123"}, runner=runner)
    assert code == 0 and len(runner.calls) == 1
    call = runner.calls[0]
    assert (call["host"], call["port"]) == ("0.0.0.0", 9123)
    with TestClient(call["app"]) as client:
        assert client.get("/healthz").status_code == 200
    default = FakeRunner()
    serve.main(environ={"PM_AGENT_ENV": "local", "PM_AGENT_COMPONENT": "mcp-verifier"}, runner=default)
    assert default.calls[0]["port"] == 8080


def test_jobs_and_lazy_asgi_targets_dispatch_through_load_target(monkeypatch: pytest.MonkeyPatch,
                                                                 settings: Settings) -> None:
    loaded: list[str] = []

    def fake_job() -> int:
        return 3

    def fake_factory(settings: Settings) -> Any:
        return {"factory_settings": settings.config_version}

    def fake_load(target: str) -> Any:
        loaded.append(target)
        return fake_job if target.endswith(":main") else fake_factory

    monkeypatch.setattr(serve, "load_target", fake_load)
    runner = FakeRunner()
    assert serve.main(environ={"PM_AGENT_ENV": "local", "PM_AGENT_COMPONENT": "mcp-correlation",
                               "PM_AGENT_JOB": "recluster"}, runner=runner) == 3
    assert serve.main(environ={"PM_AGENT_ENV": "local", "PM_AGENT_COMPONENT": "ledger-anchor"}, runner=runner) == 3
    assert serve.main(environ={"PM_AGENT_ENV": "local", "PM_AGENT_COMPONENT": "mcp-correlation",
                               "PM_AGENT_JOB": "vacuum"}, runner=runner) == 2
    assert runner.calls == []  # jobs never start a server
    app = serve.build_app(serve.resolve_component("pm-workbench"), settings, environ={})
    assert app == {"factory_settings": settings.config_version}
    assert loaded == ["pm_agent.jobs.recluster:main", "pm_agent.jobs.ledger_anchor:main",
                      "pm_agent.workbench.app:create_app"]


def test_load_target_rejects_malformed_targets() -> None:
    with pytest.raises(ValueError):
        serve.load_target("no-colon")
    assert serve.load_target("pm_agent.serve:resolve_component") is serve.resolve_component


def test_strict_environments_never_get_local_stand_ins(settings: Settings) -> None:
    stage = settings.model_copy(update={"environment": Environment.STAGE})
    with pytest.raises(PlaceholderNotImplemented):
        serve.services_for("mcp-ledger", stage, environ={})
    with pytest.raises(PlaceholderNotImplemented) as excinfo:
        serve.services_for("mcp-ado", stage, environ={})
    assert excinfo.value.question_id == "Q-10"  # SDP key material for connector redaction


def test_kms_keys_are_only_used_by_components_that_sign_or_verify(settings: Settings) -> None:
    environ = {"PM_AGENT_APPROVAL_KEY": "projects/p/locations/l/keyRings/r/cryptoKeys/k/cryptoKeyVersions/1",
               "PM_AGENT_VERIFIER_KEY": "projects/p/locations/l/keyRings/r/cryptoKeys/v/cryptoKeyVersions/1"}
    assert serve.component_environ("mcp-ado", environ) == {}
    assert serve.component_environ("mcp-ado-write", environ) == {"PM_AGENT_APPROVAL_KEY": environ[
        "PM_AGENT_APPROVAL_KEY"]}
    assert serve.component_environ("mcp-ledger", environ) == environ
    services = serve.services_for("mcp-ado", settings, environ=environ)  # no KMS call for a read connector
    assert services.ado is not None
