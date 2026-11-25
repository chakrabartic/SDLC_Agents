"""Agent-side ports — everything capability workflows and the Agent Runtime apps may depend on (ADR-003, ADR-011).

``Services`` is the dependency bundle handed to ``build_*`` workflow builders. Its I/O dependencies are Protocols
(the in-memory reference adapters satisfy them locally; AlloyDB / ``mcp-*`` clients do in production); the domain
services (Verifier, Ledger, CorrelationEngine, …) stay concrete because they are pure code with injected ports.

Two rules keep the read/write identity split of ADR-003 structural rather than conventional:

* this module, and everything LLM-side code imports, never imports the composition root (``pm_agent.runtime``);
* the agent sees the approval service only through ``ApprovalsPort`` (propose + read). Deciding, minting and
  consuming tokens live behind the ledger service — the workbench BFF and the commit executor hold that handle,
  not the agents (``runtime.LocalServices``). ``tests/architecture`` enforces both on the real import closure.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol

from pm_agent.connectors.base import SourceConnector
from pm_agent.connectors.state import IngestionState
from pm_agent.correlation.aggregates import AggregateBuilder
from pm_agent.correlation.engine import CorrelationEngine
from pm_agent.correlation.repository import ProblemStore
from pm_agent.grounding.atoms import AtomExtractor
from pm_agent.grounding.evidence_store import EvidenceRepository
from pm_agent.grounding.redaction import Redactor
from pm_agent.grounding.verifier import NearDuplicateChecker, Verifier
from pm_agent.hitl.catalog import ActionCatalog
from pm_agent.hitl.identity import IdentityVerifier
from pm_agent.hitl.ledger import Ledger
from pm_agent.hitl.subjects import SubjectStore
from pm_agent.prototype.dls import DlsCatalog
from pm_agent.schemas import CommitResult, ProposedAction, SourceSystem, VerificationReport
from pm_agent.settings import Settings

__all__ = ["ApprovalsPort", "CommitClient", "EvidenceRepository", "IngestionState", "ProblemStore", "Services",
           "SubjectStore"]


class ApprovalsPort(Protocol):
    """What an agent may do with the HITL ledger: propose an action and read its status (never decide)."""

    def propose(self, *, action_type: str, target: dict[str, Any], payload: dict[str, Any], draft_ref: str,
                report: VerificationReport, proposed_by: str, session_id: str | None = None,
                trace_id: str | None = None) -> ProposedAction: ...

    def get_action(self, action_id: str) -> ProposedAction: ...


class CommitClient(Protocol):
    """How ``pm-agent-core`` asks the zero-LLM executor to run an approved action (A2A in production)."""

    def execute(self, action_id: str) -> CommitResult: ...


@dataclass(kw_only=True)
class Services:  # pylint: disable=too-many-instance-attributes
    """The agent-side dependency bundle (one per environment, built by ``pm_agent.runtime``)."""

    settings: Settings
    clock: Callable[[], datetime]
    agent_id: str
    evidence: EvidenceRepository
    problems: ProblemStore
    subjects: SubjectStore
    redactor: Redactor
    atoms: AtomExtractor
    verifier: Verifier
    ledger: Ledger
    catalog: ActionCatalog
    approvals: ApprovalsPort
    identity: IdentityVerifier
    commit: CommitClient
    correlation: CorrelationEngine
    aggregates: AggregateBuilder
    near_duplicates: NearDuplicateChecker
    ingestion: IngestionState
    connectors: dict[SourceSystem, SourceConnector] = field(default_factory=dict)
    dls_catalog: DlsCatalog | None = None

    @property
    def organizations(self) -> list[str]:
        return sorted((self.settings.grounding.gazetteers.get("organizations") or {}).keys())
