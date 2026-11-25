"""Reference wiring of ``ServerServices`` for local runs, dev and tests (never stage/prod).

One coherent in-process composition: fixture connectors over the golden set (or HTTP connectors that fail with the
blocking Q-xx when the fixtures are absent, e.g. in images), the regex redactor (stand-in for SDP), in-memory evidence
/ ledger / drafts, the lexical judge and scorer, and token-verifying write servers backed by ``RecordingWriteBackend``.

Signing keys: local ES256 keys by default. When ``PM_AGENT_APPROVAL_KEY`` / ``PM_AGENT_VERIFIER_KEY`` name Cloud KMS
key versions (dev deployments, docs/12), those are used instead so that separately deployed servers (ledger mints,
write servers verify) agree on keys.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping
from datetime import datetime
from pathlib import Path

from pm_agent.connectors.ado import HttpAdoConnector
from pm_agent.connectors.entities import EntityResolver
from pm_agent.connectors.figma import HttpFigmaConnector
from pm_agent.connectors.fixtures import (
    FIXTURE_ROOT,
    MANIFEST,
    fixture_figma_connector,
    fixture_kb_adapter,
    golden_fixtures,
)
from pm_agent.connectors.kb import HttpKbAdapter
from pm_agent.connectors.msgraph import HttpGraphDriveConnector, HttpGraphMailConnector
from pm_agent.connectors.pagerduty import HttpPagerDutyConnector
from pm_agent.connectors.recording import RecordingWriteBackend
from pm_agent.connectors.servicenow import HttpServiceNowConnector
from pm_agent.correlation import CorrelationEngine, ProblemRepository
from pm_agent.correlation.scoring import LexicalScorer
from pm_agent.crypto import KmsSigner, LocalEcdsaSigner, Signer
from pm_agent.grounding import AtomExtractor, InMemoryEvidenceStore, LocalRedactor, PolicyEngine, Verifier
from pm_agent.grounding.dedup import jaccard_exact
from pm_agent.grounding.judge import LexicalEntailmentJudge
from pm_agent.hitl.approvals import ApprovalService
from pm_agent.hitl.catalog import ActionCatalog
from pm_agent.hitl.identity import LocalIdentityVerifier
from pm_agent.hitl.ledger import InMemoryLedgerStore, Ledger
from pm_agent.hitl.subjects import InMemorySubjectStore
from pm_agent.hitl.write_guard import WriteToolGuard
from pm_agent.mcp_servers.correlation import EngineCorrelationService
from pm_agent.mcp_servers.registry import TOKEN_GATED_TOOLS, token_gated
from pm_agent.mcp_servers.services import ServerServices
from pm_agent.mcp_servers.store import InMemoryStoreService
from pm_agent.mcp_servers.write_server import WriteToolServer
from pm_agent.placeholders import is_placeholder
from pm_agent.schemas import DuplicateCandidate, utcnow
from pm_agent.settings import ConfigurationError, Settings, load_settings

LOCAL_PSEUDONYM_KEY = b"pm-agent-local-dev-pseudonym-key"  # dev only; production pseudonyms are SDP + KMS-wrapped
APPROVAL_KEY_ENV = "PM_AGENT_APPROVAL_KEY"
VERIFIER_KEY_ENV = "PM_AGENT_VERIFIER_KEY"
SUBSCRIPTION_ENV = "PM_AGENT_SUBSCRIPTION"


class LocalNearDuplicates:
    """V10 source over records registered in memory (production: WIQL + KB search; docs/06 §6.3).

    The local counterpart of ``runtime.KnownRecordsNearDuplicates`` — this module may not import the composition
    root. Without *some* source the Verifier fails every commit-bound draft or problem on V10 (fail closed).
    """

    def __init__(self, min_similarity: float = 0.5) -> None:
        self.records: dict[str, tuple[str, str]] = {}
        self._min_similarity = min_similarity

    def add(self, ref: str, system: str, text: str) -> None:
        self.records[ref] = (system, text)

    def find(self, text: str) -> list[DuplicateCandidate]:
        """Records whose 3-shingle Jaccard with *text* reaches ``min_similarity`` (the Verifier applies its own bar)."""
        scored = ((ref, system, jaccard_exact(text, other)) for ref, (system, other) in self.records.items())
        return [DuplicateCandidate(ref=ref, system=system, similarity=round(score, 4), method="shingle")
                for ref, system, score in scored if score >= self._min_similarity]


def _signer(environ: Mapping[str, str], variable: str, local_key_id: str) -> Signer:
    name = environ.get(variable, "")
    if name and "PLACEHOLDER:" not in name and not is_placeholder(name):
        return KmsSigner(name)
    return LocalEcdsaSigner(key_id=local_key_id)


def _connectors(services: ServerServices, settings: Settings, root: Path, entities: EntityResolver) -> None:
    redactor = services.redactor
    if redactor is None:
        raise ConfigurationError("connectors need a redactor")
    if (root / MANIFEST).is_file():
        golden = golden_fixtures(redactor, settings=settings, entities=entities, root=root)
        services.ado, services.servicenow, services.pagerduty = golden.ado, golden.servicenow, golden.pagerduty
        services.mail, services.drive = golden.mail, golden.drive
        services.figma = fixture_figma_connector(redactor, root=root)
        services.kb = fixture_kb_adapter(root=root)
        return
    services.ado = HttpAdoConnector.from_settings(settings, redactor, entities=entities)
    services.servicenow = HttpServiceNowConnector.from_settings(settings, redactor, entities=entities)
    services.pagerduty = HttpPagerDutyConnector.from_settings(settings, redactor, entities=entities)
    services.mail = HttpGraphMailConnector.from_settings(settings, redactor, entities=entities)
    services.drive = HttpGraphDriveConnector.from_settings(settings, redactor, entities=entities)
    services.figma = HttpFigmaConnector.from_settings(settings, redactor)
    services.kb = HttpKbAdapter.from_settings(settings)


def local_services(settings: Settings | None = None, *, fixture_root: Path | None = None,
                   environ: Mapping[str, str] | None = None,
                   clock: Callable[[], datetime] = utcnow) -> ServerServices:
    """Everything every MCP server needs, in-process. Refuses stage/prod (local stand-ins must never back them)."""
    settings = settings or load_settings()
    if settings.strict:
        raise ConfigurationError("local_services is for local/dev only; stage/prod use production adapters")
    env = os.environ if environ is None else environ
    redactor = LocalRedactor.from_settings(settings, pseudonym_key=LOCAL_PSEUDONYM_KEY)
    atoms = AtomExtractor.from_settings(settings)
    evidence = InMemoryEvidenceStore()
    policy = PolicyEngine(settings)
    near_duplicates = LocalNearDuplicates()
    verifier = Verifier(store=evidence, judge=LexicalEntailmentJudge(), second_judge=LexicalEntailmentJudge(0.85),
                        settings=settings, signer=_signer(env, VERIFIER_KEY_ENV, "verifier-local"), redactor=redactor,
                        policy=policy, near_duplicate_checker=near_duplicates, atoms=atoms)
    ledger = Ledger(InMemoryLedgerStore(), clock=clock)
    token_signer = _signer(env, APPROVAL_KEY_ENV, "approval-token-local")
    approvals = ApprovalService(settings=settings, ledger=ledger, catalog=ActionCatalog.from_settings(settings),
                                token_signer=token_signer,
                                verifier_keys={verifier.signer.key_id: verifier.signer.public_key_pem()},
                                identity=LocalIdentityVerifier(LocalEcdsaSigner(key_id="iap-local"), clock=clock),
                                policy=policy, clock=clock)
    guard = WriteToolGuard(keys={token_signer.key_id: token_signer.public_key_pem()},
                           audience=settings.hitl.token_audience, issuer=settings.hitl.token_issuer,
                           leeway_seconds=settings.hitl.clock_skew_seconds, clock=clock)
    backends = {server: RecordingWriteBackend(system=server) for server in sorted({s for s, _ in TOKEN_GATED_TOOLS})}
    write_servers = {server: WriteToolServer(server, guard, {tool: backend.handler(tool)
                                                             for tool in token_gated(server)})
                     for server, backend in backends.items()}
    problems = ProblemRepository()
    engine = CorrelationEngine(settings, evidence, problems, LexicalScorer(settings, atoms))
    services = ServerServices(
        settings=settings, redactor=redactor, verifier=verifier, policy=policy, near_duplicates=near_duplicates,
        approvals=approvals, ledger=ledger,
        store=InMemoryStoreService(evidence, InMemorySubjectStore(), redactor),
        correlation=EngineCorrelationService(engine, evidence, problems, subscription=env.get(SUBSCRIPTION_ENV)),
        write_servers=write_servers, write_backends=backends)
    _connectors(services, settings, fixture_root or FIXTURE_ROOT, EntityResolver.from_settings(settings))
    return services
