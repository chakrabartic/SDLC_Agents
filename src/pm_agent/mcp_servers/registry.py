"""The MCP tool registry — the source of truth for every server's tools and annotations (docs/09 §1, docs/08 §3).

Annotations are a **security contract**: Agent Gateway CEL policies allow ``AID-CORE`` / ``AID-INGEST`` only tools
with ``mcp.tool.isReadOnly == true`` and allow ``AID-COMMIT`` write tools by exact ``mcp.toolName``. The contract
test compares this registry with the Terraform toolspecs (``infra/terraform/modules/agent_registry/toolspecs``),
with the live servers built by ``pm_agent.mcp_servers.factory`` and with the HITL action catalog.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ToolSpec:
    name: str
    read_only: bool
    destructive: bool
    idempotent: bool
    open_world: bool
    description: str


def _read(name: str, description: str, *, open_world: bool = True) -> ToolSpec:
    return ToolSpec(name=name, read_only=True, destructive=False, idempotent=True, open_world=open_world,
                    description=description)


def _write(name: str, description: str, *, idempotent: bool = False, open_world: bool = True) -> ToolSpec:
    return ToolSpec(name=name, read_only=False, destructive=False, idempotent=idempotent, open_world=open_world,
                    description=description)


SERVER_TOOLS: dict[str, list[ToolSpec]] = {
    # ------------------------------------------------------------------ read connectors (readOnlyHint=true)
    "mcp-ado": [
        _read("list_changed_work_items", "WIQL delta on System.ChangedDate > cursor (ids + revs)"),
        _read("get_work_items", "Batch fetch work items (<=200 ids, relations expanded); redacted canonical Signals"),
        _read("list_comments", "Work item comments as child signals"),
        _read("search_work_items", "Prior-art search over work items"),
    ],
    "mcp-servicenow": [
        _read("list_records", "Table API delta read (incident, sc_request, sc_req_item)"),
        _read("list_journal", "sys_journal_field comments/work notes for a record"),
    ],
    "mcp-pagerduty": [
        _read("list_incidents", "Incidents changed since cursor"),
        _read("list_notes", "Incident notes"),
        _read("list_log_entries", "Incident log entries"),
    ],
    "mcp-msgraph": [
        _read("mail_delta", "Mail delta for designated mailboxes (Exchange RBAC for Applications)"),
        _read("get_message", "Single message (redacted)"),
        _read("drive_delta", "OneDrive/SharePoint delta for Sites.Selected sites"),
        _read("get_item_text", "Document text extraction"),
        _read("read_workbook", "Tracker rows from an Excel workbook"),
    ],
    "mcp-figma": [
        _read("search_dls", "Search the SLB DLS catalog snapshot"),
        _read("get_component", "Component / component set metadata by key"),
        _read("get_file_nodes", "File node tree (REST read)"),
        _read("render_images", "Render node screenshots via REST"),
    ],
    "mcp-kb": [
        _read("search_articles", "KB-first search (backend Q-01)"),
    ],
    # ------------------------------------------------------------- write servers (token-gated, readOnlyHint=false)
    "mcp-ado-write": [
        _write("create_work_item", "Create an approved work item (validateOnly pre-flight supported)"),
        _write("add_relations", "Add Related/Hyperlink relations to an approved work item"),
    ],
    "mcp-kb-write": [
        _write("publish_article", "Publish an approved knowledge article"),
    ],
    "mcp-figma-write": [
        _write("post_comment", "Post an approved validation comment"),
    ],
    "mcp-msgraph-write": [
        _write("send_mail", "Send approved internal outreach from the single shared mailbox (Q-14)"),
    ],
    "svc-prototype-renderer": [
        _write("release_prototype_spec",
               "Release an approved PrototypeSpec to the private SLB Figma plugin (token-gated, ADR-006)"),
    ],
    # ------------------------------------------------------------------------------ internal (no open world)
    "mcp-verifier": [
        _read("redact", "SDP redaction + pseudonymization (no external side effects)", open_world=False),
        _read("check_claims", "Fast/full claim verification (V1-V5)", open_world=False),
        _read("verify_subject",
              "Full verification (V1-V10); returns a KMS-signed VerificationReport (no external side effects)",
              open_world=False),
        _read("near_duplicates", "Near-duplicate detection (V10)", open_world=False),
        _read("policy_evaluate", "Policy-as-code evaluation for an action", open_world=False),
    ],
    "mcp-ledger": [
        _write("propose", "Record a ProposedAction (requires a PASS VerificationReport)", open_world=False),
        _read("query", "Ledger query", open_world=False),
        _read("get_action", "Fetch a ProposedAction + payload by action_id", open_world=False),
        _write("decide", "PM decision (called by the Workbench BFF with the IAP JWT, never by agents)",
               open_world=False),
        _write("consume_token", "Atomically consume the approval token (single use)", open_world=False),
        _write("record", "Append COMMIT_SUCCEEDED / COMMIT_FAILED entries", open_world=False),
        _write("release_validation_package",
               "PROTOTYPE_RELEASE_INTERNAL: release an approved validation package (token-gated)", open_world=False),
        _write("record_trace_links", "ROADMAP_TRACE_LINK: record approved traceability links (token-gated)",
               open_world=False),
    ],
    "mcp-store": [
        _write("put_signals", "Chunk, embed and index redacted signals", idempotent=True, open_world=False),
        _read("evidence_search", "Hybrid vector + lexical evidence retrieval", open_world=False),
        _write("save_draft", "Save a versioned draft", open_world=False),
        _read("trace_query", "Traceability query", open_world=False),
        _write("trace_propose_links", "Propose traceability links (PM decides)", open_world=False),
        _read("run_named_query", "Run a named, parameterized analytics query", open_world=False),
    ],
    "mcp-correlation": [
        _read("find_candidates", "ANN candidate problems for a signal", open_world=False),
        _write("assign_candidates", "Score and auto-link new signals; enqueue adjudication", idempotent=True,
               open_world=False),
        _read("related", "Related signals/problems", open_world=False),
    ],
}

READ_SERVERS = frozenset({"mcp-ado", "mcp-servicenow", "mcp-pagerduty", "mcp-msgraph", "mcp-figma", "mcp-kb"})
WRITE_SERVERS = frozenset({"mcp-ado-write", "mcp-kb-write", "mcp-figma-write", "mcp-msgraph-write",
                           "svc-prototype-renderer"})
INTERNAL_SERVERS = frozenset({"mcp-verifier", "mcp-ledger", "mcp-store", "mcp-correlation"})

# Write tools that take (payload, action_id, approval_token, validate_only) and verify the approval token before
# any effect — one per action type in the HITL catalog (docs/07 §2).
TOKEN_GATED_TOOLS: frozenset[tuple[str, str]] = frozenset({
    ("mcp-ado-write", "create_work_item"),
    ("mcp-ado-write", "add_relations"),
    ("mcp-kb-write", "publish_article"),
    ("svc-prototype-renderer", "release_prototype_spec"),
    ("mcp-figma-write", "post_comment"),
    ("mcp-ledger", "release_validation_package"),
    ("mcp-msgraph-write", "send_mail"),
    ("mcp-ledger", "record_trace_links"),
})


def tool_spec(server: str, tool: str) -> ToolSpec:
    """The registry entry for ``server.tool`` (``KeyError`` if unknown)."""
    for spec in SERVER_TOOLS[server]:
        if spec.name == tool:
            return spec
    raise KeyError(f"{server} has no tool {tool}")


def token_gated(server: str) -> list[str]:
    return sorted(tool for owner, tool in TOKEN_GATED_TOOLS if owner == server)
