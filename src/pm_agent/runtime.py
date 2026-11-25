"""Composition root — builds every dependency of the ADK apps for one environment (docs/03, docs/12, ADR-011).

``build_local_services`` wires the complete system in-process for local runs, demos and component tests:
in-memory stores, local ES256 keys (stand-ins for Cloud KMS), the regex redactor (stand-in for SDP), recording
write backends behind token-verifying write servers, and the in-process commit executor (stand-in for A2A to the
``commit-executor`` Agent Runtime app). Stage/prod adapters (AlloyDB, KMS, SDP, Agent Gateway MCP client, A2A) are
wired by ``build_services`` and fail loudly on unresolved placeholders.

Dependency rule: this module may import anything; only deployable entry points import it (``tests/architecture``).
Workflows receive the agent-side ``pm_agent.ports.Services`` view; ``LocalServices`` adds the handles only the
workbench BFF, the executor app, the renderer and tests may hold (approval decisions, write servers, executor).
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING

from pm_agent.connectors.base import SourceConnector
from pm_agent.connectors.recording import RecordingWriteBackend
from pm_agent.connectors.state import InMemoryIngestionState
from pm_agent.correlation import AggregateBuilder, AggregateRecomputer, CorrelationEngine, ProblemRepository
from pm_agent.correlation.scoring import make_scorer
from pm_agent.crypto import LocalEcdsaSigner
from pm_agent.gateway import InProcessToolGateway
from pm_agent.grounding import AtomExtractor, EntailmentJudge, InMemoryEvidenceStore, LocalRedactor, Verifier
from pm_agent.grounding.dedup import jaccard_exact
from pm_agent.grounding.judge import LexicalEntailmentJudge
from pm_agent.hitl.approvals import ApprovalService
from pm_agent.hitl.catalog import ActionCatalog
from pm_agent.hitl.executor import CommitExecutor
from pm_agent.hitl.identity import LocalIdentityVerifier
from pm_agent.hitl.ledger import InMemoryLedgerStore, Ledger, LedgerStore
from pm_agent.hitl.subjects import InMemorySubjectStore
from pm_agent.hitl.write_guard import WriteToolGuard
from pm_agent.mcp_servers.write_server import WriteToolServer
from pm_agent.placeholders import not_implemented
from pm_agent.ports import Services
from pm_agent.prototype.dls import DlsCatalog, load_dls_catalog
from pm_agent.renderer.backend import RendererBackend
from pm_agent.schemas import CommitResult, DuplicateCandidate, SourceSystem, utcnow
from pm_agent.settings import ConfigurationError, Settings, load_settings

if TYPE_CHECKING:
    from fastapi import FastAPI

LOCAL_AGENT_ID = "spiffe://local.pm-agent/pm-agent-core"
LOCAL_EXECUTOR_ID = "spiffe://local.pm-agent/commit-executor"
LOCAL_PSEUDONYM_KEY = b"pm-agent-local-dev-pseudonym-key"  # dev only; production keys are KMS-wrapped (SDP)
RENDERER_SERVER = "svc-prototype-renderer"


class InProcessCommitClient:
    def __init__(self, executor: CommitExecutor, executor_id: str = LOCAL_EXECUTOR_ID) -> None:
        self._executor = executor
        self._executor_id = executor_id

    def execute(self, action_id: str) -> CommitResult:
        return self._executor.execute(action_id, executor_id=self._executor_id)


class A2aCommitClient:
    """Production: A2A call to the ``commit-executor`` Agent Runtime app through Agent Gateway egress."""

    def __init__(self, agent_card_url: str) -> None:
        self._card = agent_card_url

    def execute(self, action_id: str) -> CommitResult:
        del action_id
        not_implemented("Q-10", f"A2A client for commit-executor ({self._card}) via Agent Gateway egress")


class KnownRecordsNearDuplicates:
    """V10 source over existing records (local: ADO/KB titles registered in memory; prod: WIQL + KB search)."""

    def __init__(self) -> None:
        self.records: dict[str, tuple[str, str]] = {}

    def add(self, ref: str, system: str, text: str) -> None:
        self.records[ref] = (system, text)

    def find(self, text: str) -> list[DuplicateCandidate]:
        """Records whose 3-shingle Jaccard with *text* is at least 0.5 (the Verifier applies its own threshold)."""
        hits = []
        for ref, (system, other) in self.records.items():
            score = jaccard_exact(text, other)
            if score >= 0.5:
                hits.append(DuplicateCandidate(ref=ref, system=system, similarity=round(score, 4), method="shingle"))
        return hits


@dataclass(kw_only=True)
class LocalServices(Services):
    """The in-process system: the agent-side view plus the handles that sit on the other side of the HITL boundary.

    ``approval_service`` is the same object the agent reaches through ``approvals`` (``ApprovalsPort``); here it is
    typed in full because the workbench BFF records decisions and the executor consumes tokens with it.
    """

    approval_service: ApprovalService
    executor: CommitExecutor
    write_backends: dict[str, RecordingWriteBackend] = field(default_factory=dict)
    write_servers: dict[str, WriteToolServer] = field(default_factory=dict)


def _write_servers(catalog: ActionCatalog, guard: WriteToolGuard, subjects: InMemorySubjectStore
                   ) -> tuple[dict[str, RecordingWriteBackend], dict[str, WriteToolServer]]:
    """One token-verifying write server per catalog server, each over a recording backend (renderer: compiler)."""
    backends: dict[str, RecordingWriteBackend] = {RENDERER_SERVER: RendererBackend(subjects)}
    servers: dict[str, WriteToolServer] = {}
    for server in sorted(catalog.write_servers()):
        backend = backends.setdefault(server, RecordingWriteBackend(system=server))
        tools = [spec.tool for spec in catalog.specs() if spec.server == server]
        servers[server] = WriteToolServer(server, guard, {tool: backend.handler(tool) for tool in tools})
    return backends, servers


def build_local_services(settings: Settings | None = None, *, judge: EntailmentJudge | None = None,  # pylint: disable=too-many-locals
                         second_judge: EntailmentJudge | None = None, clock: Callable[[], datetime] = utcnow,
                         connectors: Mapping[SourceSystem, SourceConnector] | None = None,
                         ledger_store: LedgerStore | None = None,
                         dls_catalog: DlsCatalog | None = None) -> LocalServices:
    """Everything in-process. Refuses strict environments: local stand-ins must never back stage/prod."""
    settings = settings or load_settings()
    if settings.strict:
        raise ConfigurationError("build_local_services is for local/dev only; use build_services in stage/prod")
    evidence = InMemoryEvidenceStore()
    dls = dls_catalog or load_dls_catalog()
    atoms = AtomExtractor.from_settings(settings)
    redactor = LocalRedactor.from_settings(settings, pseudonym_key=LOCAL_PSEUDONYM_KEY)
    near_dups = KnownRecordsNearDuplicates()
    verifier = Verifier(store=evidence, judge=judge or LexicalEntailmentJudge(),
                        second_judge=second_judge or LexicalEntailmentJudge(threshold=0.85), settings=settings,
                        signer=LocalEcdsaSigner(key_id="verifier-local"), redactor=redactor,
                        aggregate_checker=AggregateRecomputer(evidence, atoms), near_duplicate_checker=near_dups,
                        dls_component_keys=dls.keys, atoms=atoms)
    ledger = Ledger(ledger_store or InMemoryLedgerStore(), clock=clock)
    catalog = ActionCatalog.from_settings(settings)
    identity = LocalIdentityVerifier(LocalEcdsaSigner(key_id="iap-local"), clock=clock)
    token_signer = LocalEcdsaSigner(key_id="approval-token-local")
    approvals = ApprovalService(settings=settings, ledger=ledger, catalog=catalog, token_signer=token_signer,
                                verifier_keys={verifier.signer.key_id: verifier.signer.public_key_pem()},
                                identity=identity, clock=clock)
    token_keys = {token_signer.key_id: token_signer.public_key_pem()}
    guard = WriteToolGuard(keys=token_keys, audience=settings.hitl.token_audience, issuer=settings.hitl.token_issuer,
                           leeway_seconds=settings.hitl.clock_skew_seconds, clock=clock)
    subjects = InMemorySubjectStore()
    backends, servers = _write_servers(catalog, guard, subjects)
    executor = CommitExecutor(approvals=approvals, verifier=verifier, subjects=subjects, catalog=catalog,
                              gateway=InProcessToolGateway(servers), token_keys=token_keys, settings=settings,
                              clock=clock)
    problems = ProblemRepository()
    return LocalServices(settings=settings, clock=clock, agent_id=LOCAL_AGENT_ID, evidence=evidence,
                         problems=problems, subjects=subjects, redactor=redactor, atoms=atoms, verifier=verifier,
                         ledger=ledger, catalog=catalog, approvals=approvals, identity=identity,
                         commit=InProcessCommitClient(executor),
                         correlation=CorrelationEngine(settings, evidence, problems, make_scorer(settings, atoms)),
                         aggregates=AggregateBuilder(evidence, atoms), near_duplicates=near_dups,
                         ingestion=InMemoryIngestionState(),
                         connectors=dict(connectors or {}), dls_catalog=dls, approval_service=approvals,
                         executor=executor, write_backends=backends, write_servers=servers)


def build_services(settings: Settings | None = None) -> LocalServices:
    """Environment-aware entry point used by the deployed apps.

    local/dev: everything in-process; in a repository checkout the synthetic golden connectors are attached so the
    ingestion app has sources to poll (never in images, which exclude ``tests/``).
    """
    settings = settings or load_settings()
    if not settings.strict:
        services = build_local_services(settings)
        from pm_agent.connectors import fixtures  # pylint: disable=import-outside-toplevel

        if (fixtures.FIXTURE_ROOT / fixtures.MANIFEST).is_file():
            services.connectors.update(fixtures.golden_connectors(services.redactor, settings=settings))
        return services
    not_implemented("Q-10", "stage/prod adapters: AlloyDB stores, Cloud KMS signers, SDP redactor, Gemini judges, "
                            "Agent Gateway MCP client and A2A commit client (see docs/12)")


def renderer_app(settings: Settings | None = None) -> FastAPI:
    """``svc-prototype-renderer`` wired from this environment (``python -m pm_agent.serve`` ASGI target).

    Local/dev serve the in-process renderer backend and its token-gated write server; stage/prod need a spec-store
    client (``mcp-store``) and fail loudly until it exists (Q-10).
    """
    from pm_agent.renderer.app import create_app  # pylint: disable=import-outside-toplevel  # FastAPI + MCP SDK

    services = build_services(settings)
    backend = services.write_backends.get(RENDERER_SERVER)
    if not isinstance(backend, RendererBackend):
        raise ConfigurationError("the renderer backend is not wired in this environment")
    return create_app(backend, settings=services.settings, write_server=services.write_servers.get(RENDERER_SERVER))
