"""Scheduled jobs: ledger anchoring, DLS catalog sync and re-clustering proposals."""
from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from pm_agent.connectors.base import make_signal
from pm_agent.correlation import ProblemRepository
from pm_agent.grounding import InMemoryEvidenceStore, LocalRedactor
from pm_agent.grounding.chunking import chunk_signal
from pm_agent.hitl.ledger import InMemoryLedgerStore, Ledger
from pm_agent.jobs import dls_catalog_sync, ledger_anchor, recluster
from pm_agent.schemas import Actor, Claim, ClaimKind, LedgerEntryType, LinkType, ProductProblem, RecordType, \
    SignalLink, SourceSystem
from tests.support import local_settings

AGENT = Actor(type="agent", id="spiffe://agents.example/pm-agent-core")


def test_ledger_anchor_emits_head_and_fails_loudly_on_tampering(capsys: pytest.CaptureFixture[str]) -> None:
    store = InMemoryLedgerStore()
    ledger = Ledger(store)
    for index in range(3):
        ledger.record(LedgerEntryType.ACTION_PROPOSED, actor=AGENT, payload={"action_id": f"act_{index}"})
    assert ledger_anchor.anchor(ledger) == 0
    record = json.loads(capsys.readouterr().out.strip())
    assert record["verified"] and record["seq"] == 3 and record["anchor_type"] == ledger_anchor.ANCHOR_TYPE
    store.entries_by_seq[2] = store.entries_by_seq[2].model_copy(update={"payload": {"action_id": "forged"}})
    assert ledger_anchor.anchor(ledger) == 2
    assert json.loads(capsys.readouterr().out.strip())["severity"] == "CRITICAL"


def test_dls_catalog_sync_validates_the_sample_snapshot(capsys: pytest.CaptureFixture[str]) -> None:
    assert dls_catalog_sync.main() == 0
    record = json.loads(capsys.readouterr().out.strip())
    assert record["components"] >= 10 and record["duplicate_keys"] == 0 and record["sample"] is True


def _problem(store: InMemoryEvidenceStore, problems: ProblemRepository, text: str, extra: list[tuple[str, float]]
             ) -> ProductProblem:
    redactor = LocalRedactor.from_settings(local_settings(), pseudonym_key=b"k")
    signal = make_signal(redactor=redactor, source_system=SourceSystem.ADO, source_record_id=str(len(store.signals)),
                         source_version="1", record_type=RecordType.WORK_ITEM, raw_text=text,
                         permalink=f"https://ado.example.invalid/{len(store.signals)}",
                         captured_utc=datetime(2026, 5, 1, tzinfo=UTC))
    store.add_signal(signal)
    for chunk in chunk_signal(signal):
        store.add_chunk(chunk)
    links = [SignalLink(signal_id=signal.signal_id, link_type=LinkType.HARD_REF, score=1.0, linked_by="t")]
    links += [SignalLink(signal_id=sid, link_type=LinkType.SEMANTIC, score=score, linked_by="t") for sid, score in extra]
    problem = ProductProblem(title=Claim(text="t", kind=ClaimKind.INFERENCE),
                             classification=Claim(text="c", kind=ClaimKind.INFERENCE), linked_signals=links)
    return problems.save(problem)


def test_recluster_only_proposes_moves_for_provisional_links() -> None:
    store, problems = InMemoryEvidenceStore(), ProblemRepository()
    export = _problem(store, problems, "Bulk export of depth logs to the rig WITSML store every day.", [])
    stray = make_signal(redactor=LocalRedactor.from_settings(local_settings(), pseudonym_key=b"k"),
                        source_system=SourceSystem.EMAIL, source_record_id="m1", source_version="1",
                        record_type=RecordType.EMAIL_MESSAGE, permalink="https://mail.example.invalid/m1",
                        raw_text="Please add bulk export of depth logs to the rig WITSML store daily.")
    store.add_signal(stray)
    _problem(store, problems, "Channel TVDHD missing from TIME_SURFACE after mapping upload.", [(stray.signal_id, 0.4)])
    found = recluster.proposals(local_settings(), store, problems)
    assert found and found[0]["to_problem"] == export.problem_id
    assert found[0]["action"] == "propose_move_for_pm_confirmation"



def test_recluster_honours_configured_embedding_scorer_and_threshold() -> None:
    """``recluster.proposals`` uses the active scorer's ``assign_threshold``, never a hard-coded lexical one."""
    from types import SimpleNamespace
    from pm_agent.correlation.scoring import EmbeddingScorer, make_scorer
    from pm_agent.grounding.atoms import AtomExtractor

    vectors = {
        "bulk": [1.0, 0.0],
        "tvdhd": [0.0, 1.0],
    }

    class _FakeGenAi:
        class _Models:
            @staticmethod
            def embed_content(**kwargs: object) -> SimpleNamespace:
                text = str(kwargs["contents"][0]).lower()  # type: ignore[index]
                vec = vectors["bulk"] if "bulk" in text else vectors["tvdhd"]
                return SimpleNamespace(embeddings=[SimpleNamespace(values=vec)])

        models = _Models()

    settings = local_settings()
    scorer = EmbeddingScorer(settings, _FakeGenAi())
    assert scorer.assign_threshold == settings.correlation.assign_threshold > settings.correlation.lexical_assign_threshold

    store, problems = InMemoryEvidenceStore(), ProblemRepository()
    export = _problem(store, problems, "Bulk export of depth logs.", [])
    stray = make_signal(redactor=LocalRedactor.from_settings(settings, pseudonym_key=b"k"),
                        source_system=SourceSystem.EMAIL, source_record_id="emb1", source_version="1",
                        record_type=RecordType.EMAIL_MESSAGE, permalink="https://mail.example.invalid/emb1",
                        raw_text="Need bulk export of depth logs daily.")
    store.add_signal(stray)
    # Score 0.70 is above lexical_assign_threshold (0.55) but below embedding assign_threshold (0.85): must be proposed!
    _problem(store, problems, "Channel TVDHD missing from TIME_SURFACE.", [(stray.signal_id, 0.70)])
    found = recluster.proposals(settings, store, problems, scorer=scorer)
    assert found and found[0]["to_problem"] == export.problem_id and found[0]["score"] > 0.70

    # make_scorer selects LexicalScorer offline.
    assert make_scorer(settings, AtomExtractor.from_settings(settings)).assign_threshold == (
        settings.correlation.lexical_assign_threshold)
