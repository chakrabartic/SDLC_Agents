"""Injected dependencies of the MCP servers (``ServerServices``) and the reader protocols they rely on.

Nothing here builds a Google Cloud or vendor client: composition roots (``pm_agent.mcp_servers.local`` for local/dev,
``pm_agent.serve`` per container) construct the objects and pass them in. A server only needs the fields its tools
use; ``build_server`` fails fast with the missing field's name.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol, TypeVar

from pm_agent.connectors.base import PollResult
from pm_agent.connectors.figma import DlsComponent
from pm_agent.connectors.kb import KbAdapter
from pm_agent.connectors.recording import RecordingWriteBackend
from pm_agent.grounding.policy import PolicyEngine
from pm_agent.grounding.redaction import Redactor
from pm_agent.grounding.verifier import NearDuplicateChecker, Verifier
from pm_agent.hitl.approvals import ApprovalService
from pm_agent.hitl.ledger import Ledger
from pm_agent.mcp_servers.write_server import WriteToolServer
from pm_agent.schemas import Signal
from pm_agent.settings import Settings

T = TypeVar("T")


class AdoReader(Protocol):
    def poll_records(self, cursor: str | None, limit: int = 100) -> PollResult: ...

    def get(self, record_id: str) -> Signal | None: ...

    def comments(self, work_item_id: str) -> list[Signal]: ...

    def search(self, query: str, top: int = 10) -> list[Signal]: ...


class ServiceNowReader(Protocol):
    def poll_records(self, cursor: str | None, limit: int = 100) -> PollResult: ...

    def get(self, record_id: str) -> Signal | None: ...

    def journal(self, record_id: str) -> list[Signal]: ...


class PagerDutyReader(Protocol):
    def poll_records(self, cursor: str | None, limit: int = 100) -> PollResult: ...

    def get(self, record_id: str) -> Signal | None: ...

    def notes(self, incident: str) -> list[Signal]: ...

    def log_entries(self, incident: str) -> list[Signal]: ...


class MailReader(Protocol):
    def poll_records(self, cursor: str | None, limit: int = 100) -> PollResult: ...

    def get(self, record_id: str) -> Signal | None: ...


class DriveReader(Protocol):
    def poll_records(self, cursor: str | None, limit: int = 100) -> PollResult: ...

    def item_text(self, item_id: str) -> Signal | None: ...

    def workbook_rows(self, item_id: str, sheet: str | None = None) -> list[Signal]: ...


class FigmaReader(Protocol):
    def search_dls(self, query: str, top: int = 10) -> list[DlsComponent]: ...

    def get_component(self, key: str) -> DlsComponent | None: ...

    def file_nodes(self, file_key: str, ids: Sequence[str]) -> dict[str, Any]: ...

    def render_images(self, file_key: str, ids: Sequence[str], *, fmt: str = "png", scale: float = 2
                      ) -> dict[str, str]: ...


class StoreService(Protocol):
    """``mcp-store`` backend: evidence index, drafts, trace links, named analytics queries."""

    def put_signals(self, signals: Sequence[Signal]) -> dict[str, list[str]]: ...

    def evidence_search(self, query: str, top_k: int = 8) -> list[dict[str, Any]]: ...

    def save_draft(self, subject_type: str, draft: Mapping[str, Any]) -> dict[str, str]: ...

    def trace_query(self, *, from_id: str | None = None, to_id: str | None = None, link_type: str | None = None,
                    status: str | None = None) -> list[dict[str, Any]]: ...

    def trace_propose_links(self, links: Sequence[Mapping[str, Any]]) -> list[str]: ...

    def run_named_query(self, name: str, params: Mapping[str, Any]) -> list[dict[str, Any]]: ...


class CorrelationService(Protocol):
    """``mcp-correlation`` backend: candidate problems, auto-linking, related records."""

    def find_candidates(self, signal_id: str, top_k: int = 5) -> list[dict[str, Any]]: ...

    def assign_candidates(self, signal_ids: Sequence[str]) -> list[dict[str, Any]]: ...

    def related(self, signal_id: str) -> dict[str, Any]: ...


@dataclass
class ServerServices:  # pylint: disable=too-many-instance-attributes
    settings: Settings
    redactor: Redactor | None = None
    # read connectors
    ado: AdoReader | None = None
    servicenow: ServiceNowReader | None = None
    pagerduty: PagerDutyReader | None = None
    mail: MailReader | None = None
    drive: DriveReader | None = None
    figma: FigmaReader | None = None
    kb: KbAdapter | None = None
    # internal servers
    verifier: Verifier | None = None
    policy: PolicyEngine | None = None
    near_duplicates: NearDuplicateChecker | None = None
    approvals: ApprovalService | None = None
    ledger: Ledger | None = None
    store: StoreService | None = None
    correlation: CorrelationService | None = None
    # token-gated write cores, keyed by server name (``mcp-ado-write``, …, ``mcp-ledger`` for its internal writes)
    write_servers: Mapping[str, WriteToolServer] = field(default_factory=dict)
    # local/dev only: the recording backends behind ``write_servers`` (introspection in tests and demos)
    write_backends: Mapping[str, RecordingWriteBackend] = field(default_factory=dict)


def required(value: T | None, what: str, server: str) -> T:
    """Fail fast at build time when a server's dependency was not injected."""
    if value is None:
        raise ValueError(f"{server} requires ServerServices.{what}")
    return value
