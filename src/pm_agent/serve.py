"""Container dispatcher — ``python -m pm_agent.serve`` (deploy/Dockerfile CMD, docs/12 §2).

Every containerised component ships the same package; ``PM_AGENT_COMPONENT`` selects what runs:

* ``mcp-*`` — our MCP servers (``pm_agent.mcp_servers.factory``): Streamable HTTP at ``/mcp`` + ``GET /healthz``.
* ``signal-event-gateway`` — the webhook ingress (``pm_agent.event_gateway``).
* ``pm-workbench`` / ``svc-prototype-renderer`` — ASGI apps loaded lazily (``module:factory``).
* ``dls-catalog-sync`` / ``ledger-anchor`` — batch jobs (``module:main``); ``PM_AGENT_JOB`` (e.g. ``recluster`` on the
  ``mcp-correlation`` image) runs a job instead of the server.

HTTP components listen on ``0.0.0.0:$PORT`` (default 8080). Local/dev use the in-process reference wiring
(``pm_agent.mcp_servers.local``); stage/prod build production adapters and fail loudly (``not_implemented``) where SLB
inputs are still missing.
"""

from __future__ import annotations

import importlib
import inspect
import logging
import os
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal

from pm_agent.connectors.ado import HttpAdoConnector
from pm_agent.connectors.entities import EntityResolver
from pm_agent.connectors.figma import HttpFigmaConnector
from pm_agent.connectors.kb import HttpKbAdapter
from pm_agent.connectors.msgraph import HttpGraphDriveConnector, HttpGraphMailConnector
from pm_agent.connectors.pagerduty import HttpPagerDutyConnector
from pm_agent.connectors.servicenow import HttpServiceNowConnector
from pm_agent.crypto import KmsSigner
from pm_agent.event_gateway import InMemoryHintPublisher, PubSubHintPublisher, WebhookSecrets
from pm_agent.event_gateway import create_app as create_gateway_app
from pm_agent.event_gateway.hints import DEFAULT_HINTS_TOPIC, HintPublisher
from pm_agent.grounding.redaction import SdpRedactor
from pm_agent.hitl.write_guard import JtiRegistry, WriteToolGuard
from pm_agent.mcp_servers.factory import build_server, http_app
from pm_agent.mcp_servers.local import APPROVAL_KEY_ENV, VERIFIER_KEY_ENV, local_services
from pm_agent.mcp_servers.registry import READ_SERVERS, SERVER_TOOLS, WRITE_SERVERS
from pm_agent.mcp_servers.services import ServerServices
from pm_agent.mcp_servers.store import AlloyDbStoreService
from pm_agent.mcp_servers.write_handlers import production_write_handlers
from pm_agent.mcp_servers.write_server import WriteToolServer
from pm_agent.placeholders import PlaceholderError, is_placeholder, not_implemented, placeholder
from pm_agent.settings import Environment, Settings, load_settings

ComponentKind = Literal["mcp", "gateway", "asgi", "job"]
DEFAULT_PORT = 8080
BIND_HOST = "0.0.0.0"  # container networking; ingress (Cloud Run / GKE LB, IAP) is the perimeter
EXIT_USAGE = 2
_LOG = logging.getLogger("pm_agent.serve")


class UnknownComponent(ValueError):
    """``PM_AGENT_COMPONENT`` / ``PM_AGENT_JOB`` names nothing this image can run."""


@dataclass(frozen=True)
class Component:
    name: str
    kind: ComponentKind
    target: str | None = None  # ``module:attribute`` for lazily imported ASGI factories and jobs


COMPONENTS: dict[str, Component] = {
    "pm-workbench": Component("pm-workbench", "asgi", "pm_agent.workbench.app:create_app"),
    "signal-event-gateway": Component("signal-event-gateway", "gateway"),
    **{name: Component(name, "mcp") for name in SERVER_TOOLS if name != "svc-prototype-renderer"},
    "svc-prototype-renderer": Component("svc-prototype-renderer", "asgi", "pm_agent.runtime:renderer_app"),
    "dls-catalog-sync": Component("dls-catalog-sync", "job", "pm_agent.jobs.dls_catalog_sync:main"),
    "ledger-anchor": Component("ledger-anchor", "job", "pm_agent.jobs.ledger_anchor:main"),
}
JOBS: dict[str, str] = {"recluster": "pm_agent.jobs.recluster:main"}
# Which KMS key versions each component may touch (anything else gets local keys in local/dev wiring).
_KEY_USERS: dict[str, frozenset[str]] = {
    "mcp-ledger": frozenset({APPROVAL_KEY_ENV, VERIFIER_KEY_ENV}),
    "mcp-verifier": frozenset({VERIFIER_KEY_ENV}),
    **{name: frozenset({APPROVAL_KEY_ENV}) for name in WRITE_SERVERS},
}


def resolve_component(name: str | None) -> Component:
    """The component for ``PM_AGENT_COMPONENT`` (clear error listing every valid name otherwise)."""
    if not name:
        raise UnknownComponent(f"PM_AGENT_COMPONENT is not set (one of: {', '.join(sorted(COMPONENTS))})")
    try:
        return COMPONENTS[name]
    except KeyError:
        raise UnknownComponent(f"unknown component {name!r} (one of: {', '.join(sorted(COMPONENTS))})") from None


def resolve_job(name: str) -> Component:
    """The job for ``PM_AGENT_JOB`` (e.g. ``recluster`` on the correlation-engine image)."""
    target = JOBS.get(name)
    if target is None:
        raise UnknownComponent(f"unknown PM_AGENT_JOB {name!r} (one of: {', '.join(sorted(JOBS))})")
    return Component(f"job:{name}", "job", target)


def load_target(target: str) -> Any:
    """Import ``module:attribute`` lazily (lead-owned apps and jobs are only imported in their own image)."""
    module_name, separator, attribute = target.partition(":")
    if not separator or not module_name or not attribute:
        raise ValueError(f"target must be 'module:attribute', got {target!r}")
    return getattr(importlib.import_module(module_name), attribute)


def component_environ(component: str, environ: Mapping[str, str]) -> dict[str, str]:
    """The slice of the environment a component's local/dev wiring may use (KMS keys only where needed)."""
    allowed = _KEY_USERS.get(component, frozenset())
    sensitive = {APPROVAL_KEY_ENV, VERIFIER_KEY_ENV}
    return {key: value for key, value in environ.items() if key not in sensitive or key in allowed}


# ------------------------------------------------------------------------------------------- services
def _sdp_redactor(settings: Settings, environ: Mapping[str, str]) -> SdpRedactor:
    wrapped, kms_key = environ.get("PM_AGENT_SDP_WRAPPED_KEY", ""), environ.get("PM_AGENT_SDP_KMS_KEY", "")
    if not wrapped or not kms_key or is_placeholder(wrapped) or is_placeholder(kms_key):
        not_implemented("Q-10", "SDP de-identification key material (KMS-wrapped key) for connector redaction")
    return SdpRedactor(settings.gcp.project_id, settings.gcp.location, wrapped, kms_key,
                       custom_infotypes=dict(settings.grounding.custom_infotypes))


def _kms_guard(settings: Settings, environ: Mapping[str, str], *,
               registry: JtiRegistry | None = None) -> WriteToolGuard:
    key_version = environ.get(APPROVAL_KEY_ENV, "")
    if not key_version or "PLACEHOLDER:" in key_version:
        not_implemented("Q-10", "approval-token-signer KMS key version (PM_AGENT_APPROVAL_KEY)")
    if registry is None:
        # Cloud Run scales write servers across instances, so an in-process jti set cannot enforce single use.
        not_implemented("Q-10", "durable jti registry (Memorystore SET NX / AlloyDB hitl.token_executions) "
                                "for write-server single-use enforcement")
    signer = KmsSigner(key_version)
    return WriteToolGuard(keys={signer.key_id: signer.public_key_pem()}, audience=settings.hitl.token_audience,
                          issuer=settings.hitl.token_issuer, leeway_seconds=settings.hitl.clock_skew_seconds,
                          registry=registry)


def _read_connectors(component: str, settings: Settings, services: ServerServices, environ: Mapping[str, str]
                     ) -> None:
    redactor = _sdp_redactor(settings, environ)
    entities = EntityResolver.from_settings(settings)
    services.redactor = redactor
    if component == "mcp-ado":
        services.ado = HttpAdoConnector.from_settings(settings, redactor, entities=entities)
    elif component == "mcp-servicenow":
        services.servicenow = HttpServiceNowConnector.from_settings(settings, redactor, entities=entities)
    elif component == "mcp-pagerduty":
        services.pagerduty = HttpPagerDutyConnector.from_settings(settings, redactor, entities=entities)
    elif component == "mcp-msgraph":
        services.mail = HttpGraphMailConnector.from_settings(settings, redactor, entities=entities)
        services.drive = HttpGraphDriveConnector.from_settings(settings, redactor, entities=entities)
    elif component == "mcp-figma":
        services.figma = HttpFigmaConnector.from_settings(settings, redactor)
    else:
        services.kb = HttpKbAdapter.from_settings(settings)


def production_services(component: str, settings: Settings, environ: Mapping[str, str]) -> ServerServices:
    """Stage/prod wiring for one MCP server; unresolved SLB inputs raise ``PlaceholderNotImplemented``."""
    services = ServerServices(settings=settings)
    if component in READ_SERVERS:
        _read_connectors(component, settings, services, environ)
    elif component in WRITE_SERVERS:
        services.write_servers = {component: WriteToolServer(component, _kms_guard(settings, environ),
                                                             production_write_handlers(component, settings))}
    elif component == "mcp-store":
        services.store = AlloyDbStoreService(environ.get("PM_AGENT_ALLOYDB_INSTANCE")
                                             or placeholder("Q-10", "alloydb_instance"))
    else:
        not_implemented("Q-10", f"stage/prod adapters for {component}: Cloud KMS signers, AlloyDB ledger/stores, "
                                "SDP, Gemini judges and the correlation index (docs/12)")
    return services


def services_for(component: str, settings: Settings, environ: Mapping[str, str]) -> ServerServices:
    """Local/dev: in-process reference wiring; stage/prod: production adapters for this component only."""
    if settings.strict:
        return production_services(component, settings, environ)
    return local_services(settings, environ=component_environ(component, environ))


# ----------------------------------------------------------------------------------------------- apps
def _publisher(settings: Settings, environ: Mapping[str, str]) -> HintPublisher:
    if settings.environment is Environment.LOCAL:
        return InMemoryHintPublisher()
    return PubSubHintPublisher.from_settings(settings, topic=environ.get("PM_AGENT_HINTS_TOPIC") or DEFAULT_HINTS_TOPIC)


def build_app(component: Component, settings: Settings, environ: Mapping[str, str] | None = None) -> Any:
    """The ASGI application for an HTTP component."""
    env = os.environ if environ is None else environ
    if component.kind == "mcp":
        return http_app(build_server(component.name, services_for(component.name, settings, env)))
    if component.kind == "gateway":
        return create_gateway_app(settings, _publisher(settings, env), WebhookSecrets.from_env(env))
    if component.kind == "asgi" and component.target:
        factory = load_target(component.target)
        accepts_settings = "settings" in inspect.signature(factory).parameters
        return factory(settings=settings) if accepts_settings else factory()
    raise UnknownComponent(f"{component.name} is not an HTTP component")


def run_job(component: Component) -> int:
    """Run a batch job's ``main()``; its integer return value (or 0) is the exit code."""
    if component.target is None:
        raise UnknownComponent(f"{component.name} has no job entry point")
    result = load_target(component.target)()
    return result if isinstance(result, int) else 0


def _uvicorn_run(app: Any, **kwargs: Any) -> None:
    import uvicorn  # pylint: disable=import-outside-toplevel

    uvicorn.run(app, **kwargs)


def main(argv: Sequence[str] | None = None, environ: Mapping[str, str] | None = None, *,
         runner: Callable[..., None] | None = None) -> int:
    """Dispatch on ``PM_AGENT_JOB`` / ``PM_AGENT_COMPONENT`` (``argv[0]`` overrides the component)."""
    env = os.environ if environ is None else environ
    logging.basicConfig(level=env.get("PM_AGENT_LOG_LEVEL", "INFO"))
    try:
        job = env.get("PM_AGENT_JOB")
        if job:
            return run_job(resolve_job(job))
        requested = argv[0] if argv else env.get("PM_AGENT_COMPONENT")
        component = resolve_component(requested)
        if component.kind == "job":
            return run_job(component)
        settings = load_settings(environ=env)
        app = build_app(component, settings, env)
    except UnknownComponent as exc:
        _LOG.error("%s", exc)
        return EXIT_USAGE
    except PlaceholderError as exc:
        _LOG.error("cannot start: %s", exc)
        raise
    port = int(env.get("PORT") or DEFAULT_PORT)
    (runner or _uvicorn_run)(app, host=BIND_HOST, port=port, proxy_headers=True,
                             access_log=component.kind != "gateway", log_level="info")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
