"""Composition root — builds every dependency of the ADK apps for one environment (docs/03, docs/12).

``build_local_services`` wires the complete system in-process for local runs, demos and component tests:
in-memory stores, local ES256 keys (stand-ins for Cloud KMS), the regex redactor (stand-in for SDP), recording
write backends behind token-verifying write servers, and the in-process commit executor (stand-in for A2A to the
``commit-executor`` Agent Runtime app). Stage/prod adapters (AlloyDB, KMS, SDP, Agent Gateway MCP client, A2A) are
wired by ``build_services`` and fail loudly on unresolved placeholders.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol

from pm_agent.connectors.base import SourceConnector
from pm_agent.connectors.recording import RecordingWriteBackend
from pm_agent.correlation import AggregateBuilder, AggregateRecomputer, CorrelationEngine, ProblemRepository
from pm_agent.correlation.scoring import EmbeddingScorer, LexicalScorer, SimilarityScorer
from pm_agent.crypto import LocalEcdsaSigner
from pm_agent.gateway import InProcessToolGateway, ToolServer
from pm_agent.grounding import (
    AtomExtractor,
    EntailmentJudge,
    InMemoryEvidenceStore,
    LocalRedactor,
    Redactor,
    Verifier,
)
from pm_agent.grounding.dedup import jaccard_exact
from pm_agent.grounding.judge import LexicalEntailmentJudge
from pm_agent.hitl import (
    ActionCatalog,
    ApprovalService,
    CommitExecutor,
    CommitResult,
    IdentityVerifier,
    InMemoryLedgerStore,
    InMemorySubjectStore,
    Ledger,
    LedgerStore,
    LocalIdentityVerifier,
    WriteToolGuard,
)
from pm_agent.mcp_servers.write_server import WriteToolServer
from pm_agent.placeholders import not_implemented
from pm_agent.prototype.dls import DlsCatalog, load_dls_catalog
from pm_agent.renderer.backend import RendererBackend
from pm_agent.schemas import DuplicateCandidate, SourceSystem, VerificationReport, utcnow
from pm_agent.settings import ConfigurationError, Settings, load_settings

LOCAL_AGENT_ID = "spiffe://local.pm-agent/pm-agent-core"
LOCAL_EXECUTOR_ID = "spiffe://local.pm-agent/commit-executor"
LOCAL_PSEUDONYM_KEY = b"pm-agent-local-dev-pseudonym-key"  # dev only; production keys are KMS-wrapped (SDP)


class CommitClient(Protocol):
    """How ``pm-agent-core`` asks the zero-LLM executor to run an approved action (A2A in production)."""

    def execute(self, action_id: str) -> CommitResult: ...


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


@dataclass
class Services:  # pylint: disable=too-many-instance-attributes
    settings: Settings
    clock: Callable[[], datetime]
    evidence: InMemoryEvidenceStore
    problems: ProblemRepository
    subjects: InMemorySubjectStore
    redactor: Redactor
    atoms: AtomExtractor
    verifier: Verifier
    ledger: Ledger
    catalog: ActionCatalog
    approvals: ApprovalService
    identity: IdentityVerifier
    commit: CommitClient
    correlation: CorrelationEngine
    aggregates: AggregateBuilder
    near_duplicates: KnownRecordsNearDuplicates
    agent_id: str = LOCAL_AGENT_ID
    connectors: dict[SourceSystem, SourceConnector] = field(default_factory=dict)
    write_backends: dict[str, RecordingWriteBackend] = field(default_factory=dict)
    executor: CommitExecutor | None = None
    dls_catalog: DlsCatalog | None = None
    reports: dict[str, VerificationReport] = field(default_factory=dict)

    @property
    def dls_component_keys(self) -> frozenset[str]:
        return self.dls_catalog.keys if self.dls_catalog else frozenset()

    @property
    def organizations(self) -> list[str]:
        return sorted((self.settings.grounding.gazetteers.get("organizations") or {}).keys())


def make_scorer(settings: Settings, atoms: AtomExtractor) -> SimilarityScorer:
    """``correlation.scoring``: ``embedding`` (gemini-embedding, needs ADC) or ``lexical`` (offline reference)."""
    if settings.correlation.scoring == "embedding":
        from pm_agent.settings import make_genai_client  # pylint: disable=import-outside-toplevel

        return EmbeddingScorer(settings, make_genai_client(settings))
    return LexicalScorer(settings, atoms)


def build_local_services(settings: Settings | None = None, *, judge: EntailmentJudge | None = None,  # pylint: disable=too-many-locals
                         second_judge: EntailmentJudge | None = None, clock: Callable[[], datetime] = utcnow,
                         connectors: Mapping[SourceSystem, SourceConnector] | None = None,
                         ledger_store: LedgerStore | None = None,
                         dls_catalog: DlsCatalog | None = None) -> Services:
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
    backends: dict[str, RecordingWriteBackend] = {"svc-prototype-renderer": RendererBackend(subjects)}
    servers: dict[str, ToolServer] = {}
    for server in sorted(catalog.write_servers()):
        backend = backends.setdefault(server, RecordingWriteBackend(system=server))
        tools = [spec.tool for spec in catalog.specs() if spec.server == server]
        servers[server] = WriteToolServer(server, guard, {tool: backend.handler(tool) for tool in tools})
    executor = CommitExecutor(approvals=approvals, verifier=verifier, subjects=subjects, catalog=catalog,
                              gateway=InProcessToolGateway(servers), token_keys=token_keys, settings=settings,
                              clock=clock)
    problems = ProblemRepository()
    return Services(settings=settings, clock=clock, evidence=evidence, problems=problems, subjects=subjects,
                    redactor=redactor, atoms=atoms, verifier=verifier, ledger=ledger, catalog=catalog,
                    approvals=approvals, identity=identity, commit=InProcessCommitClient(executor),
                    correlation=CorrelationEngine(settings, evidence, problems, make_scorer(settings, atoms)),
                    aggregates=AggregateBuilder(evidence, atoms), near_duplicates=near_dups,
                    connectors=dict(connectors or {}), write_backends=backends, executor=executor,
                    dls_catalog=dls)


def build_services(settings: Settings | None = None) -> Services:
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
