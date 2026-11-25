"""Tool implementations per MCP server: thin, typed adapters from MCP arguments to injected services.

Every builder returns ``{tool_name: function}``; ``pm_agent.mcp_servers.factory`` registers them with the registry's
annotations and translates typed failures into MCP tool errors. Outputs are JSON objects; signals are always the
connectors' redacted canonical ``Signal``s with permalinks, and list tools page with opaque cursors.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from dataclasses import asdict
from typing import Any, TypeVar

from pm_agent.connectors.base import PollResult
from pm_agent.connectors.common import checked_limit, decode_offset_cursor, encode_offset_cursor
from pm_agent.grounding.policy import PolicyEngine
from pm_agent.mcp_servers.registry import token_gated
from pm_agent.mcp_servers.services import ServerServices, required
from pm_agent.mcp_servers.write_server import WriteToolServer
from pm_agent.schemas import ClaimDraft, Decision, LedgerEntryType, Signal, VerificationReport
from pm_agent.schemas.subjects import SUBJECT_TYPES

ToolFn = Callable[..., dict[str, Any]]
T = TypeVar("T")
MAX_IDS = 200


def signals_out(signals: Iterable[Signal]) -> list[dict[str, Any]]:
    return [signal.model_dump(mode="json") for signal in signals]


def poll_out(result: PollResult) -> dict[str, Any]:
    return {"signals": signals_out(result.signals), "next_cursor": result.next_cursor, "status": result.status.value,
            "reason": result.reason}


def paged(items: Sequence[T], cursor: str | None, page_size: int) -> tuple[list[T], str | None]:
    """Offset paging over an in-memory list with opaque ``fx:`` cursors (``None`` when exhausted)."""
    start = decode_offset_cursor(cursor)
    end = start + checked_limit(page_size)
    return list(items[start:end]), (encode_offset_cursor(end) if end < len(items) else None)


def _signal_page(signals: Sequence[Signal], cursor: str | None, page_size: int) -> dict[str, Any]:
    page, next_cursor = paged(signals, cursor, page_size)
    return {"signals": signals_out(page), "next_cursor": next_cursor}


def _ids(ids: Sequence[int | str]) -> list[str]:
    if len(ids) > MAX_IDS:
        raise ValueError(f"at most {MAX_IDS} ids per call")
    return [str(i) for i in ids]


# ------------------------------------------------------------------------------------------ read servers
def ado_tools(services: ServerServices) -> dict[str, ToolFn]:
    """``mcp-ado``: change delta (ids + revs), batch reads, comments as child signals, prior-art search."""
    ado = required(services.ado, "ado", "mcp-ado")

    def list_changed_work_items(cursor: str | None = None, page_size: int = 100) -> dict[str, Any]:
        result = ado.poll_records(cursor, page_size)
        return {"items": [{"id": s.source_record_id, "rev": s.source_version, "signal_id": s.signal_id,
                           "changed_utc": s.captured_utc.isoformat()} for s in result.signals],
                "next_cursor": result.next_cursor, "status": result.status.value}

    def get_work_items(ids: list[int | str]) -> dict[str, Any]:
        found, missing = [], []
        for work_item_id in _ids(ids):
            signal = ado.get(work_item_id)
            if signal is None:
                missing.append(work_item_id)
            else:
                found.append(signal)
        return {"signals": signals_out(found), "missing": missing}

    def list_comments(work_item_id: int | str, cursor: str | None = None, page_size: int = 100) -> dict[str, Any]:
        return _signal_page(ado.comments(str(work_item_id)), cursor, page_size)

    def search_work_items(query: str, top: int = 10) -> dict[str, Any]:
        return {"signals": signals_out(ado.search(query, checked_limit(top)))}

    return {"list_changed_work_items": list_changed_work_items, "get_work_items": get_work_items,
            "list_comments": list_comments, "search_work_items": search_work_items}


def servicenow_tools(services: ServerServices) -> dict[str, ToolFn]:
    """``mcp-servicenow``: Table API delta and journal (comments / work notes) per record."""
    snow = required(services.servicenow, "servicenow", "mcp-servicenow")

    def list_records(cursor: str | None = None, page_size: int = 100) -> dict[str, Any]:
        return poll_out(snow.poll_records(cursor, page_size))

    def list_journal(record_id: str, cursor: str | None = None, page_size: int = 100) -> dict[str, Any]:
        return _signal_page(snow.journal(record_id), cursor, page_size)

    return {"list_records": list_records, "list_journal": list_journal}


def pagerduty_tools(services: ServerServices) -> dict[str, ToolFn]:
    """``mcp-pagerduty``: incidents since the cursor, notes and log entries per incident."""
    pagerduty = required(services.pagerduty, "pagerduty", "mcp-pagerduty")

    def list_incidents(cursor: str | None = None, page_size: int = 100) -> dict[str, Any]:
        return poll_out(pagerduty.poll_records(cursor, page_size))

    def list_notes(incident_id: int | str, cursor: str | None = None, page_size: int = 100) -> dict[str, Any]:
        return _signal_page(pagerduty.notes(str(incident_id)), cursor, page_size)

    def list_log_entries(incident_id: int | str, cursor: str | None = None, page_size: int = 100) -> dict[str, Any]:
        return _signal_page(pagerduty.log_entries(str(incident_id)), cursor, page_size)

    return {"list_incidents": list_incidents, "list_notes": list_notes, "list_log_entries": list_log_entries}


def msgraph_tools(services: ServerServices) -> dict[str, ToolFn]:
    """``mcp-msgraph``: mail delta, single messages, drive delta, document text and tracker rows."""
    mail = required(services.mail, "mail", "mcp-msgraph")
    drive = required(services.drive, "drive", "mcp-msgraph")

    def mail_delta(cursor: str | None = None, page_size: int = 100) -> dict[str, Any]:
        return poll_out(mail.poll_records(cursor, page_size))

    def get_message(message_id: str) -> dict[str, Any]:
        signal = mail.get(message_id)
        if signal is None:
            raise LookupError(f"message {message_id} not found")
        return {"signal": signal.model_dump(mode="json")}

    def drive_delta(cursor: str | None = None, page_size: int = 100) -> dict[str, Any]:
        return poll_out(drive.poll_records(cursor, page_size))

    def get_item_text(item_id: str) -> dict[str, Any]:
        signal = drive.item_text(item_id)
        if signal is None:
            raise LookupError(f"no extracted text for drive item {item_id}")
        return {"signal": signal.model_dump(mode="json")}

    def read_workbook(item_id: str, sheet: str | None = None) -> dict[str, Any]:
        return {"signals": signals_out(drive.workbook_rows(item_id, sheet))}

    return {"mail_delta": mail_delta, "get_message": get_message, "drive_delta": drive_delta,
            "get_item_text": get_item_text, "read_workbook": read_workbook}


def figma_tools(services: ServerServices) -> dict[str, ToolFn]:
    """``mcp-figma``: DLS catalog search, component metadata, file nodes and rendered screenshots."""
    figma = required(services.figma, "figma", "mcp-figma")
    required(services.redactor, "redactor", "mcp-figma")  # fail closed if redaction wiring is missing (DM-5)

    def search_dls(query: str, top: int = 10) -> dict[str, Any]:
        return {"components": [asdict(c) for c in figma.search_dls(query, checked_limit(top))]}

    def get_component(key: str) -> dict[str, Any]:
        component = figma.get_component(key)
        if component is None:
            raise LookupError(f"component {key} not found")
        return {"component": asdict(component)}

    def get_file_nodes(file_key: str, ids: list[str]) -> dict[str, Any]:
        return {"file": figma.file_nodes(file_key, _ids(ids))}

    def render_images(file_key: str, ids: list[str], image_format: str = "png", scale: float = 2) -> dict[str, Any]:
        return {"images": figma.render_images(file_key, _ids(ids), fmt=image_format, scale=scale)}

    return {"search_dls": search_dls, "get_component": get_component, "get_file_nodes": get_file_nodes,
            "render_images": render_images}


def kb_tools(services: ServerServices) -> dict[str, ToolFn]:
    """``mcp-kb``: KB-first search through the backend-agnostic adapter (hits redacted before return)."""
    kb = required(services.kb, "kb", "mcp-kb")
    redactor = required(services.redactor, "redactor", "mcp-kb")

    def search_articles(query: str, top: int = 10, labels: str | None = None) -> dict[str, Any]:
        filters = {"labels": labels} if labels else None
        hits = [hit.as_dict() for hit in kb.search(query, filters, checked_limit(top))]
        for hit in hits:  # outputs are always redacted server-side before return (docs/09 §1)
            hit["title"], hit["snippet"] = redactor.redact(hit["title"]).text, redactor.redact(hit["snippet"]).text
        return {"hits": hits}

    return {"search_articles": search_articles}


# ----------------------------------------------------------------------------------------- write servers
def write_tool(core: WriteToolServer, tool: str) -> ToolFn:
    """MCP face of one token-gated write tool: the token is verified by ``WriteToolServer`` before any effect."""

    def call(payload: dict[str, Any], action_id: str, approval_token: str,
             validate_only: bool = False) -> dict[str, Any]:
        return core.call(tool, {"payload": payload, "action_id": action_id, "approval_token": approval_token,
                                "validate_only": validate_only})

    call.__name__ = tool
    call.__doc__ = f"Token-gated write: {tool} (approval token verified before any vendor call)."
    return call


def write_tools(server: str, services: ServerServices) -> dict[str, ToolFn]:
    core = services.write_servers.get(server)
    if core is None:
        raise ValueError(f"{server} requires ServerServices.write_servers[{server!r}]")
    missing = sorted(set(token_gated(server)) - set(core.tools))
    if missing:
        raise ValueError(f"{server} write core lacks handlers for {missing}")
    return {tool: write_tool(core, tool) for tool in token_gated(server)}


# -------------------------------------------------------------------------------------- internal servers
def verifier_tools(services: ServerServices) -> dict[str, ToolFn]:
    """``mcp-verifier``: redaction, claim checks (V1–V5), signed subject verification, V10 and policy."""
    verifier = required(services.verifier, "verifier", "mcp-verifier")
    redactor = required(services.redactor, "redactor", "mcp-verifier")
    policy = services.policy or PolicyEngine(services.settings)

    def redact(text: str) -> dict[str, Any]:
        result = redactor.redact(text)
        return {"text": result.text, "findings": result.findings}

    def check_claims(claims: list[dict[str, Any]]) -> dict[str, Any]:
        result = verifier.check_claims([ClaimDraft.model_validate(c) for c in claims])
        return {"passed": [c.model_dump(mode="json") for c in result.passed],
                "blocked": [asdict(b) for b in result.blocked]}

    def verify_subject(subject: dict[str, Any], subject_type: str, commit_bound: bool = False,
                       target_action: str | None = None, target_payload: dict[str, Any] | None = None
                       ) -> dict[str, Any]:
        model_cls = SUBJECT_TYPES.get(subject_type)
        if model_cls is None:
            raise ValueError(f"unknown subject type {subject_type!r}")
        report = verifier.verify_subject(model_cls.model_validate(subject), subject_type=subject_type,
                                         commit_bound=commit_bound, target_action=target_action,
                                         target_payload=target_payload)
        return report.model_dump(mode="json")

    def near_duplicates(text: str) -> dict[str, Any]:
        checker = services.near_duplicates
        candidates = checker.find(text) if checker is not None else []
        return {"candidates": [c.model_dump(mode="json") for c in candidates], "source_configured": checker is not None}

    def policy_evaluate(action_type: str, payload: dict[str, Any], target: dict[str, Any] | None = None
                        ) -> dict[str, Any]:
        decision = policy.evaluate(action_type, payload, target or {})
        return {"allowed": decision.allowed, "rule_ids": decision.rule_ids, "reasons": decision.reasons}

    return {"redact": redact, "check_claims": check_claims, "verify_subject": verify_subject,
            "near_duplicates": near_duplicates, "policy_evaluate": policy_evaluate}


def ledger_tools(services: ServerServices) -> dict[str, ToolFn]:
    """``mcp-ledger``: the HITL protocol. ``decide`` never returns the token; only ``consume_token`` does."""
    approvals = required(services.approvals, "approvals", "mcp-ledger")
    ledger = required(services.ledger, "ledger", "mcp-ledger")

    def propose(action_type: str, target: dict[str, Any], payload: dict[str, Any], draft_ref: str,
                report: dict[str, Any], proposed_by: str, session_id: str | None = None,
                trace_id: str | None = None) -> dict[str, Any]:
        action = approvals.propose(action_type=action_type, target=target, payload=payload, draft_ref=draft_ref,
                                   report=VerificationReport.model_validate(report), proposed_by=proposed_by,
                                   session_id=session_id, trace_id=trace_id)
        return action.model_dump(mode="json")

    def query(entry_types: list[str] | None = None, subject_type: str | None = None, subject_id: str | None = None,
              action_id: str | None = None, limit: int = 200) -> dict[str, Any]:
        entries = ledger.query(entry_types=[LedgerEntryType(t) for t in entry_types] if entry_types else None,
                               subject_type=subject_type, subject_id=subject_id, action_id=action_id)
        return {"entries": [e.model_dump(mode="json") for e in entries[-checked_limit(limit):]]}

    def get_action(action_id: str) -> dict[str, Any]:
        return approvals.get_action(action_id).model_dump(mode="json")

    def decide(action_id: str, decision: str, assertion: str, comment: str = "",
               expected_version: int | None = None) -> dict[str, Any]:
        outcome = approvals.decide(action_id=action_id, decision=Decision(decision), assertion=assertion,
                                   comment=comment, expected_version=expected_version)
        return {"action": outcome.action.model_dump(mode="json"), "token_issued": outcome.token_issued,
                "approvals": outcome.approvals, "required": outcome.required}

    def consume_token(action_id: str, consumer: str) -> dict[str, Any]:
        issued = approvals.consume_token(action_id=action_id, consumer=consumer)
        return {"token": issued.token, "claims": issued.claims.model_dump(mode="json")}

    def record(action_id: str, actor_id: str, succeeded: bool, external_ref: dict[str, str] | None = None,
               error: str | None = None, changes_requested: bool = False, report_id: str | None = None
               ) -> dict[str, Any]:
        action = approvals.record_commit(action_id=action_id, actor_id=actor_id, succeeded=succeeded,
                                         external_ref=external_ref, error=error,
                                         changes_requested=changes_requested, report_id=report_id)
        return action.model_dump(mode="json")

    return {"propose": propose, "query": query, "get_action": get_action, "decide": decide,
            "consume_token": consume_token, "record": record, **write_tools("mcp-ledger", services)}


def store_tools(services: ServerServices) -> dict[str, ToolFn]:
    """``mcp-store``: idempotent signal indexing, evidence search, drafts, trace links, named queries."""
    store = required(services.store, "store", "mcp-store")

    def put_signals(signals: list[dict[str, Any]]) -> dict[str, Any]:
        return dict(store.put_signals([Signal.model_validate(s) for s in signals]))

    def evidence_search(query: str, top_k: int = 8) -> dict[str, Any]:
        return {"hits": store.evidence_search(query, checked_limit(top_k))}

    def save_draft(subject_type: str, draft: dict[str, Any]) -> dict[str, Any]:
        return dict(store.save_draft(subject_type, draft))

    def trace_query(from_id: str | None = None, to_id: str | None = None, link_type: str | None = None,
                    status: str | None = None) -> dict[str, Any]:
        return {"links": store.trace_query(from_id=from_id, to_id=to_id, link_type=link_type, status=status)}

    def trace_propose_links(links: list[dict[str, Any]]) -> dict[str, Any]:
        return {"link_ids": store.trace_propose_links(links)}

    def run_named_query(name: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        return {"rows": store.run_named_query(name, params or {})}

    return {"put_signals": put_signals, "evidence_search": evidence_search, "save_draft": save_draft,
            "trace_query": trace_query, "trace_propose_links": trace_propose_links,
            "run_named_query": run_named_query}


def correlation_tools(services: ServerServices) -> dict[str, ToolFn]:
    """``mcp-correlation``: candidate problems, auto-link / adjudication routing, related records."""
    correlation = required(services.correlation, "correlation", "mcp-correlation")

    def find_candidates(signal_id: str, top_k: int = 5) -> dict[str, Any]:
        return {"candidates": correlation.find_candidates(signal_id, checked_limit(top_k))}

    def assign_candidates(signal_ids: list[str]) -> dict[str, Any]:
        return {"assignments": correlation.assign_candidates(_ids(signal_ids))}

    def related(signal_id: str) -> dict[str, Any]:
        return correlation.related(signal_id)

    return {"find_candidates": find_candidates, "assign_candidates": assign_candidates, "related": related}
