"""Vendor write handlers behind the token-gated write servers (docs/07 §2, docs/09 §2–§7).

A handler runs **after** ``WriteToolServer`` verified the approval token for this exact tool, payload and action. It
performs exactly one vendor call (or only the pre-flight when ``validate_only``) and returns ``{status, external_id,
url}``. ``target`` is the approved ``tgt`` claim; handlers refuse to act on a different org/project/system.

Real vendor connectivity is blocked on SLB inputs, so the default transports fail with the open question
(ADO Q-04, Figma Q-02, Graph Q-14 shared mailbox / Q-13 tenant scope, KB backend Q-01). Local/dev wiring uses
``RecordingWriteBackend`` instead (``pm_agent.mcp_servers.local``).

Open items: ``PLACEHOLDER:Q-04:ado_write_credentials``, ``PLACEHOLDER:Q-02:figma_comment_oauth``,
``PLACEHOLDER:Q-13:msgraph_mail_send_credentials``.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from pm_agent.connectors.ado import AdoApi, ado_permalink
from pm_agent.connectors.figma import FigmaApi, figma_comment_permalink
from pm_agent.connectors.http import Transport, UnconfiguredTransport
from pm_agent.connectors.kb import HttpKbAdapter, KbAdapter, PublishRequiresWorkflow
from pm_agent.connectors.msgraph import GraphApi
from pm_agent.gateway import ToolCallError
from pm_agent.grounding.policy import PolicyEngine
from pm_agent.mcp_servers.write_server import WriteHandler
from pm_agent.placeholders import is_placeholder, not_implemented
from pm_agent.schemas import ActionType, TraceLink
from pm_agent.settings import Settings

MAX_COMMENT_CHARS = 5000


def _object(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, Mapping):
        raise ToolCallError("validation_failed", "payload must be a JSON object")
    return dict(payload)


def _field(payload: Mapping[str, Any], name: str) -> Any:
    if payload.get(name) in (None, "", [], {}):
        raise ToolCallError("validation_failed", f"payload.{name} is required")
    return payload[name]


def _check_target(target: Mapping[str, Any], system: str, **expected: str) -> None:
    """The approved ``tgt`` must name this server's system (and org/project where given)."""
    if target.get("system") not in (None, system):
        raise ToolCallError("unauthorized", f"approved target system {target.get('system')!r} is not {system!r}")
    for key, value in expected.items():
        if target.get(key) not in (None, value):
            raise ToolCallError("unauthorized", f"approved target {key} does not match this server")


def ado_write_handlers(*, organization: str, project: str, transport: Transport | None = None,
                       api_version: str = "7.1") -> dict[str, WriteHandler]:
    """``create_work_item`` (JSON Patch, ``validateOnly`` pre-flight, ``pm-agent:<action>`` tag) + ``add_relations``."""
    api = AdoApi(organization, project, api_version)
    sender = transport or UnconfiguredTransport("Q-04", "ado-write")

    def create_work_item(*, payload: Any, target: dict[str, Any], validate_only: bool,
                         idempotency_key: str) -> dict[str, Any]:
        body = _object(payload)
        _check_target(target, "ado", org=organization, project=project)
        fields = _field(body, "fields")
        if not isinstance(fields, Mapping):
            raise ToolCallError("validation_failed", "payload.fields must be an object")
        try:
            request = api.create_work_item(str(_field(body, "work_item_type")), dict(fields),
                                           validate_only=validate_only, relations=list(body.get("relations") or []),
                                           idempotency_tag=f"pm-agent:{idempotency_key}")
        except ValueError as exc:
            raise ToolCallError("validation_failed", str(exc)) from exc
        response = sender.send(request)
        if validate_only:
            return {"status": "valid"}
        work_item_id = str(response.get("id"))
        return {"status": "created", "external_id": work_item_id,
                "url": ado_permalink(organization, project, work_item_id), "rev": str(response.get("rev"))}

    def add_relations(*, payload: Any, target: dict[str, Any], validate_only: bool,
                      idempotency_key: str) -> dict[str, Any]:
        del idempotency_key  # relation existence check + test /rev make the PATCH safe to retry
        body = _object(payload)
        _check_target(target, "ado", org=organization, project=project)
        try:
            request = api.add_relations(int(_field(body, "work_item_id")), rev=int(_field(body, "rev")),
                                        relations=list(_field(body, "relations")), validate_only=validate_only)
        except ValueError as exc:
            raise ToolCallError("validation_failed", str(exc)) from exc
        response = sender.send(request)
        if validate_only:
            return {"status": "valid"}
        work_item_id = str(response.get("id") or body["work_item_id"])
        return {"status": "linked", "external_id": work_item_id,
                "url": ado_permalink(organization, project, work_item_id)}

    return {"create_work_item": create_work_item, "add_relations": add_relations}


def kb_write_handlers(adapter: KbAdapter) -> dict[str, WriteHandler]:
    """``publish_article``: adapter ``validate`` as pre-flight; ``create_draft`` + ``publish`` (Q-01 backend)."""

    def publish_article(*, payload: Any, target: dict[str, Any], validate_only: bool,
                        idempotency_key: str) -> dict[str, Any]:
        del idempotency_key  # the adapter's external id is the idempotency anchor (docs/07 §2)
        draft = _object(payload)
        _check_target(target, "kb")
        checked = adapter.validate(draft)
        if not checked.ok:
            raise ToolCallError("validation_failed", "; ".join(checked.problems))
        if validate_only:
            return {"status": "valid"}
        published = adapter.publish(adapter.create_draft(draft))
        if isinstance(published, PublishRequiresWorkflow):
            return {"status": "submitted", "external_id": published.ref.external_id, "url": published.ref.url,
                    "workflow": published.workflow}
        return {"status": "published", "external_id": published.external_id, "url": published.url}

    return {"publish_article": publish_article}


def figma_write_handlers(*, transport: Transport | None = None) -> dict[str, WriteHandler]:
    """``post_comment`` with an ``[pm-agent:<action>]`` marker (idempotency); pre-flight = payload lint."""
    api = FigmaApi()
    sender = transport or UnconfiguredTransport("Q-02", "figma-write")

    def post_comment(*, payload: Any, target: dict[str, Any], validate_only: bool,
                     idempotency_key: str) -> dict[str, Any]:
        body = _object(payload)
        _check_target(target, "figma")
        file_key, message = str(_field(body, "file_key")), str(_field(body, "message"))
        if len(message) > MAX_COMMENT_CHARS:
            raise ToolCallError("validation_failed", f"comment exceeds {MAX_COMMENT_CHARS} characters")
        node_id = str(body["node_id"]) if body.get("node_id") else None
        request = api.post_comment(file_key, f"{message}\n\n[pm-agent:{idempotency_key}]", node_id=node_id)
        if validate_only:
            return {"status": "valid"}
        comment_id = str(sender.send(request).get("id"))
        return {"status": "created", "external_id": comment_id,
                "url": figma_comment_permalink(file_key, comment_id, node_id)}

    return {"post_comment": post_comment}


def msgraph_write_handlers(*, shared_mailbox: str, policy: PolicyEngine,
                           transport: Transport | None = None) -> dict[str, WriteHandler]:
    """``send_mail`` from the single shared mailbox; pre-flight = recipient policy (P-03, internal only)."""
    api = GraphApi()
    sender = transport or UnconfiguredTransport("Q-13", "msgraph-write")

    def send_mail(*, payload: Any, target: dict[str, Any], validate_only: bool,
                  idempotency_key: str) -> dict[str, Any]:
        body = _object(payload)
        _check_target(target, "msgraph")
        decision = policy.evaluate(ActionType.OUTREACH_SEND_INTERNAL.value, body, target)
        if not decision.allowed:
            raise ToolCallError("validation_failed", "; ".join(decision.reasons))
        if validate_only:
            return {"status": "valid"}
        if not shared_mailbox or is_placeholder(shared_mailbox):
            not_implemented("Q-14", "outreach shared mailbox for send_mail")
        sender.send(api.send_mail(shared_mailbox, to=[str(r) for r in body.get("to") or []],
                                  cc=[str(r) for r in body.get("cc") or []], subject=str(_field(body, "subject")),
                                  body=str(_field(body, "body")), action_id=idempotency_key))
        return {"status": "sent", "external_id": idempotency_key}

    return {"send_mail": send_mail}


def renderer_write_handlers() -> dict[str, WriteHandler]:
    """``release_prototype_spec``: pre-flight checks the spec compiles; release needs the private plugin (Q-02)."""

    def release_prototype_spec(*, payload: Any, target: dict[str, Any], validate_only: bool,
                               idempotency_key: str) -> dict[str, Any]:
        del idempotency_key, target
        spec = _object(payload)
        screens = _field(spec, "screens")
        if not isinstance(screens, list):
            raise ToolCallError("validation_failed", "payload.screens must be a list")
        if validate_only:
            return {"status": "valid"}
        not_implemented("Q-02", "private SLB Figma plugin release channel for approved PrototypeSpecs (ADR-006)")

    return {"release_prototype_spec": release_prototype_spec}


def ledger_write_handlers() -> dict[str, WriteHandler]:
    """Internal writes recorded by the ledger itself (the commit executor records COMMIT_SUCCEEDED with the ref)."""

    def release_validation_package(*, payload: Any, target: dict[str, Any], validate_only: bool,
                                   idempotency_key: str) -> dict[str, Any]:
        del target, idempotency_key
        package_id = str(_field(_object(payload), "package_id"))
        if validate_only:
            return {"status": "valid"}
        return {"status": "released", "external_id": package_id,
                "url": f"pm-agent://validation-packages/{package_id}"}

    def record_trace_links(*, payload: Any, target: dict[str, Any], validate_only: bool,
                           idempotency_key: str) -> dict[str, Any]:
        del target
        links = _field(_object(payload), "links")
        try:
            validated = [TraceLink.model_validate(link) for link in links]
        except (TypeError, ValueError) as exc:
            raise ToolCallError("validation_failed", "payload.links must be TraceLink objects") from exc
        if validate_only:
            return {"status": "valid"}
        return {"status": "recorded", "external_id": idempotency_key,
                "url": f"pm-agent://trace-links/{idempotency_key}", "links": [link.link_id for link in validated]}

    return {"release_validation_package": release_validation_package, "record_trace_links": record_trace_links}


def production_write_handlers(server: str, settings: Settings, *, kb: KbAdapter | None = None,
                              transport: Transport | None = None) -> dict[str, WriteHandler]:
    """The vendor handlers for one write server (stage/prod wiring)."""
    if server == "mcp-ado-write":
        cfg = settings.connectors.get("ado") or {}
        return ado_write_handlers(organization=str(cfg.get("organization", "")), project=str(cfg.get("project", "")),
                                  transport=transport, api_version=str(cfg.get("api_version") or "7.1"))
    if server == "mcp-kb-write":
        return kb_write_handlers(kb or HttpKbAdapter.from_settings(settings))
    if server == "mcp-figma-write":
        return figma_write_handlers(transport=transport)
    if server == "mcp-msgraph-write":
        return msgraph_write_handlers(shared_mailbox=str(settings.outreach.get("shared_mailbox") or ""),
                                      policy=PolicyEngine(settings), transport=transport)
    if server == "svc-prototype-renderer":
        return renderer_write_handlers()
    if server == "mcp-ledger":
        return ledger_write_handlers()
    raise ValueError(f"{server} is not a write server")
