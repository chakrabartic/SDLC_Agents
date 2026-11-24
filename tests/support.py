"""Shared offline test doubles (no network, no LLM, no Google Cloud)."""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from pm_agent.connectors.recording import RecordingWriteBackend
from pm_agent.crypto import LocalEcdsaSigner
from pm_agent.gateway import InProcessToolGateway
from pm_agent.grounding import (
    SUBJECT_TYPES,
    InMemoryEvidenceStore,
    JudgeUnavailable,
    JudgeVerdict,
    LocalRedactor,
    Verifier,
)
from pm_agent.hitl.approvals import ApprovalService
from pm_agent.hitl.catalog import ActionCatalog
from pm_agent.hitl.executor import CommitExecutor
from pm_agent.hitl.identity import LocalIdentityVerifier
from pm_agent.hitl.ledger import InMemoryLedgerStore, Ledger
from pm_agent.hitl.subjects import InMemorySubjectStore
from pm_agent.hitl.write_guard import WriteToolGuard
from pm_agent.mcp_servers.write_server import WriteToolServer
from pm_agent.schemas import (
    Approver,
    CitedQuote,
    ClaimDraft,
    ClaimKind,
    DraftField,
    EvidenceChunk,
    FieldStatus,
    ProposedAction,
    PrototypeScreen,
    PrototypeSpec,
    RequirementDraft,
    Signal,
    SourceSystem,
    SpecNode,
)
from pm_agent.settings import Settings, load_settings

PD_TEXT = ("For well WELL-0042(h), the channel TVDHD from source log TIME_RT_WITSML was not being populated "
           "into the standardized log TIME_SURFACE. Root cause: the standardization mapping file was uploaded after "
           "the channel streamed. Customer: CNE.")
PD_QUOTE = ("the channel TVDHD from source log TIME_RT_WITSML was not being populated into the standardized log "
            "TIME_SURFACE")
PD_CLAIM = "TVDHD was not populated into TIME_SURFACE."


class ScriptedJudge:
    """Deterministic judge: always returns the scripted verdict (or raises ``JudgeUnavailable``)."""

    def __init__(self, verdict: str = "entailed", fail: bool = False) -> None:
        self.verdict, self.fail, self.calls = verdict, fail, 0

    def judge(self, claim_text: str, quotes: Sequence[str]) -> JudgeVerdict:
        del claim_text
        self.calls += 1
        if self.fail:
            raise JudgeUnavailable("quota exhausted")
        return JudgeVerdict(verdict=self.verdict, supporting_quote_index=0 if quotes else None)  # type: ignore[arg-type]


class StaticNearDuplicates:
    """V10 source returning fixed candidates."""

    def __init__(self, candidates: list[Any] | None = None) -> None:
        self.candidates = candidates or []

    def find(self, text: str) -> list[Any]:
        del text
        return list(self.candidates)


def local_settings() -> Settings:
    return load_settings(environ={"PM_AGENT_ENV": "local"})


def make_store() -> InMemoryEvidenceStore:
    store = InMemoryEvidenceStore()
    seed_pd_signal(store)
    return store


def seed_pd_signal(store: InMemoryEvidenceStore) -> Signal:
    """Add the PagerDuty incident used across tests (one signal, one chunk ``ev_pd_1``)."""
    signal = Signal(source_system=SourceSystem.PAGERDUTY, source_record_id="90000001", source_version="1",
                    record_type="incident", permalink="https://example.pagerduty.com/incidents/90000001",
                    captured_utc=datetime(2026, 5, 9, tzinfo=UTC), raw_text=PD_TEXT)
    store.add_signal(signal)
    store.add_chunk(EvidenceChunk(evidence_id="ev_pd_1", signal_id=signal.signal_id, seq=0, text=PD_TEXT,
                                  char_start=0, char_end=len(PD_TEXT)))
    return signal


def make_verifier(store: InMemoryEvidenceStore, settings: Settings, *, judge: Any = None, second_judge: Any = None,
                  with_redactor: bool = True, **kwargs: Any) -> Verifier:
    redactor = LocalRedactor.from_settings(settings, pseudonym_key=b"test-key") if with_redactor else None
    return Verifier(store=store, judge=judge or ScriptedJudge(), second_judge=second_judge, settings=settings,
                    signer=LocalEcdsaSigner(key_id="verifier-test"), redactor=redactor, **kwargs)


def fact(text: str = PD_CLAIM, quote: str | None = PD_QUOTE, evidence_id: str = "ev_pd_1",
         kind: ClaimKind = ClaimKind.FACT) -> ClaimDraft:
    cites = [CitedQuote(evidence_id=evidence_id, quote=quote)] if quote else []
    return ClaimDraft(text=text, kind=kind, citations=cites)


def grounded_draft(verifier: Verifier, *drafts: ClaimDraft) -> RequirementDraft:
    """A requirement draft whose summary holds only claims that passed V1–V5."""
    checked = verifier.check_claims(list(drafts or (fact(),)))
    assert not checked.blocked, checked.blocked
    return RequirementDraft(problem_id="prb_1", work_item_type="Requirement",
                            fields={"summary": DraftField(status=FieldStatus.POPULATED, claims=checked.passed)})


def ado_payload(draft: RequirementDraft) -> dict[str, Any]:
    """The exact payload the PM approves (rendered from verified claims only)."""
    return {"work_item_type": draft.work_item_type,
            "fields": {"System.Title": draft.fields["summary"].claims[0].text}}


# ------------------------------------------------------------------------------------------- HITL harness
AGENT_ID = "spiffe://agents.example/pm-agent-core"
EXECUTOR_ID = "spiffe://agents.example/commit-executor"
PM = Approver(sub="pm-1@slb.test", email="EMAIL_PM0001", idp="entra-test", groups=["pm_approvers"])
PM2 = Approver(sub="pm-2@slb.test", email="EMAIL_PM0002", idp="entra-test", groups=["pm_approvers"])
UX = Approver(sub="ux-1@slb.test", email="EMAIL_UX0001", idp="entra-test", groups=["ux_reviewers"])
OUTSIDER = Approver(sub="dev-1@slb.test", email="EMAIL_DEV001", idp="entra-test", groups=["engineering"])
DLS_KEYS = frozenset({"dls:button/primary", "dls:card/evidence", "dls:table/default"})


class Clock:
    """Controllable UTC clock shared by the approval service, guard and executor."""

    def __init__(self) -> None:
        self.now = datetime.now(UTC)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **delta: float) -> None:
        self.now += timedelta(**delta)


def prototype_spec() -> PrototypeSpec:
    screen = PrototypeScreen(name="Approval", width=1440, height=900, nodes=[
        SpecNode(kind="instance", name="Approve", component_key="dls:button/primary"),
        SpecNode(kind="instance", name="Evidence", component_key="dls:card/evidence")])
    return PrototypeSpec(brief_id="brief_1", requirement_id="draft-req_1", dls_library_file_key="lib",
                         dls_published_version="v1", screens=[screen])


@dataclass
class Hitl:  # pylint: disable=too-many-instance-attributes
    settings: Settings
    clock: Clock
    store: InMemoryEvidenceStore
    verifier: Verifier
    ledger: Ledger
    catalog: ActionCatalog
    identities: LocalIdentityVerifier
    token_signer: LocalEcdsaSigner
    approvals: ApprovalService
    subjects: InMemorySubjectStore
    backend: RecordingWriteBackend
    guard: WriteToolGuard
    ado_server: WriteToolServer
    gateway: InProcessToolGateway
    executor: CommitExecutor

    def verify(self, subject: Any, action_type: str, payload: dict[str, Any]) -> Any:
        subject_type = next(t for t, cls in SUBJECT_TYPES.items() if isinstance(subject, cls))
        return self.verifier.verify_subject(subject, subject_type=subject_type, commit_bound=True,
                                            target_action=action_type, target_payload=payload)

    def propose(self, subject: Any, subject_type: str, action_type: str, payload: dict[str, Any]) -> ProposedAction:
        ref = self.subjects.put(subject_type, subject)
        report = self.verify(subject, action_type, payload)
        assert report.overall.value == "PASS", report.checks
        return self.approvals.propose(action_type=action_type, target={"system": "test"}, payload=payload,
                                      draft_ref=ref, report=report, proposed_by=AGENT_ID)

    def propose_ado(self) -> ProposedAction:
        draft = grounded_draft(self.verifier)
        return self.propose(draft, "requirement_draft", "ADO_CREATE_WORK_ITEM", ado_payload(draft))

    def ledger_dump(self) -> str:
        return "\n".join(e.model_dump_json() for e in self.ledger.query())


def make_hitl() -> Hitl:
    """The full propose → approve → execute chain, in-process, with recording write backends."""
    settings = local_settings()
    clock = Clock()
    store = make_store()
    verifier = make_verifier(store, settings, second_judge=ScriptedJudge(),
                             near_duplicate_checker=StaticNearDuplicates(), dls_component_keys=DLS_KEYS)
    ledger = Ledger(InMemoryLedgerStore(), clock=clock)
    catalog = ActionCatalog.from_settings(settings)
    identities = LocalIdentityVerifier(LocalEcdsaSigner(key_id="iap-test"), clock=clock)
    token_signer = LocalEcdsaSigner(key_id="approval-token-test")
    approvals = ApprovalService(settings=settings, ledger=ledger, catalog=catalog, token_signer=token_signer,
                                verifier_keys={verifier.signer.key_id: verifier.signer.public_key_pem()},
                                identity=identities, clock=clock)
    token_keys = {token_signer.key_id: token_signer.public_key_pem()}
    guard = WriteToolGuard(keys=token_keys, audience=settings.hitl.token_audience,
                           issuer=settings.hitl.token_issuer, clock=clock)
    backend = RecordingWriteBackend(system="ado")
    ado_server = WriteToolServer("mcp-ado-write", guard, {"create_work_item": backend.handler("create_work_item"),
                                                          "add_relations": backend.handler("add_relations")})
    figma_backend = RecordingWriteBackend(system="figma")
    renderer = WriteToolServer("svc-prototype-renderer", guard,
                               {"release_prototype_spec": figma_backend.handler("release_prototype_spec")})
    gateway = InProcessToolGateway({"mcp-ado-write": ado_server, "svc-prototype-renderer": renderer})
    subjects = InMemorySubjectStore()
    executor = CommitExecutor(approvals=approvals, verifier=verifier, subjects=subjects, catalog=catalog,
                              gateway=gateway, token_keys=token_keys, settings=settings, clock=clock)
    return Hitl(settings=settings, clock=clock, store=store, verifier=verifier, ledger=ledger, catalog=catalog,
                identities=identities, token_signer=token_signer, approvals=approvals, subjects=subjects,
                backend=backend, guard=guard, ado_server=ado_server, gateway=gateway, executor=executor)
