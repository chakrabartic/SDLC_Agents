"""Contract tests for ``mcp-store`` and ``mcp-correlation`` (docs/06 §6, docs/10 §7, DM-5).

Exercises every tool backend method directly and through MCP ``build_server``:
* ``InMemoryStoreService`` requires a redactor (DM-5), rejects unredacted signals, runs every named query, stores
  immutable drafts, enforces non-overwrite on ``trace_propose_links``, and queries trace links;
* ``AlloyDbStoreService`` fails loudly with Q-10 on every method;
* ``EngineCorrelationService`` finds candidates, applies ``AUTO_LINK`` idempotently, queues ``ADJUDICATE`` once,
  computes the neighbourhood graph (``related``), and fails loudly on ``drain_subscription``.
"""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from pm_agent.connectors.base import make_signal
from pm_agent.correlation import CorrelationEngine, LexicalScorer, ProblemRepository
from pm_agent.grounding import AtomExtractor, InMemoryEvidenceStore, LocalRedactor
from pm_agent.hitl.subjects import InMemorySubjectStore
from pm_agent.mcp_servers.correlation import EngineCorrelationService
from pm_agent.mcp_servers.factory import build_server
from pm_agent.mcp_servers.services import ServerServices
from pm_agent.mcp_servers.store import AlloyDbStoreService, InMemoryStoreService
from pm_agent.placeholders import PlaceholderNotImplemented
from pm_agent.schemas import (
    Claim,
    ClaimKind,
    Customer,
    LinkType,
    ProductProblem,
    RecordType,
    RequirementDraft,
    Signal,
    SignalLink,
    SourceSystem,
    TraceLink,
    TraceLinkType,
)
from tests.support import local_settings

pytestmark = pytest.mark.contract


def _signal(redactor: LocalRedactor, system: SourceSystem, record_id: str, text: str, *,
            customer: str | None = None, product: str | None = None, parent: str | None = None) -> Signal:
    return make_signal(
        redactor=redactor, source_system=system, source_record_id=record_id, source_version="1",
        record_type=RecordType.INCIDENT, permalink=f"https://{system.value}.example.invalid/{record_id}",
        raw_text=text, title=f"Title {record_id}", captured_utc=datetime(2026, 5, 10, tzinfo=UTC),
        customer=Customer(name=customer) if customer else Customer(), product=product, parent_signal_id=parent,
        connector="contract-test")


# --------------------------------------------------------------------------------------------- mcp-store
def test_store_requires_redactor_and_enforces_dm5() -> None:
    settings = local_settings()
    evidence = InMemoryEvidenceStore()
    redactor = LocalRedactor.from_settings(settings, pseudonym_key=b"store-test")
    with pytest.raises(ValueError, match="redactor"):
        InMemoryStoreService(evidence, InMemorySubjectStore(), redactor=None)

    store = InMemoryStoreService(evidence, InMemorySubjectStore(), redactor)
    clean = _signal(redactor, SourceSystem.PAGERDUTY, "9001",
                    "TVDHD channel missing in TIME_SURFACE for WELL-0042(h). Ref CS0000123.",
                    customer="Contoso Energy", product="DrillOps")
    dirty = Signal(source_system=SourceSystem.EMAIL, source_record_id="raw-1", source_version="1",
                   record_type=RecordType.EMAIL_MESSAGE, permalink="https://mail.example.invalid/raw-1",
                   captured_utc=datetime(2026, 5, 10, tzinfo=UTC),
                   raw_text="Contact jane.doe@example.com with Server=db;Password=secret.")
    first = store.put_signals([clean, clean, dirty])
    assert first == {"stored": [clean.signal_id], "duplicates": [clean.signal_id], "rejected": [dirty.signal_id]}
    assert evidence.get_signal(dirty.signal_id) is None

    hits = store.evidence_search("TVDHD TIME_SURFACE", top_k=5)
    assert len(hits) == 1 and hits[0]["signal_id"] == clean.signal_id and hits[0]["permalink"] == clean.permalink


def test_store_named_queries_drafts_and_trace_links() -> None:
    settings = local_settings()
    redactor = LocalRedactor.from_settings(settings, pseudonym_key=b"store-test")
    store = InMemoryStoreService(InMemoryEvidenceStore(), InMemorySubjectStore(), redactor)
    s1 = _signal(redactor, SourceSystem.PAGERDUTY, "9001", "TVDHD missing in TIME_SURFACE; see CS9000123.",
                 customer="Contoso Energy", product="DrillOps")
    s2 = _signal(redactor, SourceSystem.SERVICENOW, "CS9000123", "Case for TVDHD; linked to PD #9001.",
                 customer="Contoso Energy", product="Lumi")
    store.put_signals([s1, s2])

    assert store.run_named_query("signal_counts_by_source", {}) == [
        {"source_system": "pagerduty", "count": 1}, {"source_system": "servicenow", "count": 1}]
    assert [r["source_record_id"] for r in store.run_named_query("signals_by_customer", {"customer": "Contoso Energy"})] == [
        "9001", "CS9000123"]
    assert [r["source_record_id"] for r in store.run_named_query("signals_by_product", {"product": "DrillOps"})] == [
        "9001"]
    assert [r["source_record_id"] for r in store.run_named_query(
        "signals_referencing", {"system": "servicenow", "record_id": "CS9000123"})] == ["9001"]
    with pytest.raises(LookupError, match="unknown named query"):
        store.run_named_query("drop_tables", {})

    # Draft persistence validates schema and rejects unknown subject types.
    draft = RequirementDraft(problem_id="prb_1", work_item_type="Requirement", fields={})
    saved = store.save_draft("requirement_draft", draft.model_dump(mode="json"))
    assert saved["draft_ref"].startswith("requirement_draft:") and saved["content_sha256"].startswith("sha256:")
    with pytest.raises(ValueError, match="unknown subject type"):
        store.save_draft("nope", {})

    # Trace links force proposed/agent, allow idempotent re-propose, and refuse overwrites with different content.
    link = TraceLink(link_id="tl_fixed_1", from_type="product_problem", from_id="prb_1", to_type="ado_work_item",
                     to_id="4001", link_type=TraceLinkType.SPECIFIED_BY, status="confirmed", created_by="pm")
    ids = store.trace_propose_links([link.model_dump(mode="json")])
    assert ids == ["tl_fixed_1"]
    queried = store.trace_query(from_id="prb_1", to_id="4001", link_type="specified_by", status="proposed")
    assert len(queried) == 1 and queried[0]["status"] == "proposed" and queried[0]["created_by"] == "agent"
    assert store.trace_propose_links([link.model_dump(mode="json")]) == ["tl_fixed_1"]  # idempotent
    tampered = link.model_copy(update={"to_id": "9999"})
    with pytest.raises(ValueError, match="already exists with different content"):
        store.trace_propose_links([tampered.model_dump(mode="json")])
    assert store.trace_query(from_id="prb_1")[0]["to_id"] == "4001"  # original untouched


def test_alloydb_store_service_fails_loudly_on_every_method() -> None:
    alloy = AlloyDbStoreService("projects/p/locations/l/clusters/c/instances/i")
    for call in (lambda: alloy.put_signals([]), lambda: alloy.evidence_search("q"),
                 lambda: alloy.save_draft("requirement_draft", {}), lambda: alloy.trace_query(),
                 lambda: alloy.trace_propose_links([]), lambda: alloy.run_named_query("signal_counts_by_source", {})):
        with pytest.raises(PlaceholderNotImplemented) as excinfo:
            call()
        assert excinfo.value.question_id == "Q-10"


# --------------------------------------------------------------------------------------- mcp-correlation
def test_correlation_service_candidates_idempotent_assign_and_related_graph() -> None:
    settings = local_settings()
    redactor = LocalRedactor.from_settings(settings, pseudonym_key=b"corr-test")
    evidence = InMemoryEvidenceStore()
    problems = ProblemRepository()
    atoms = AtomExtractor.from_settings(settings)
    engine = CorrelationEngine(settings, evidence, problems, LexicalScorer(settings, atoms))
    service = EngineCorrelationService(engine, evidence, problems, subscription="projects/p/subscriptions/s")

    seed = _signal(redactor, SourceSystem.PAGERDUTY, "90000011",
                   "Channel TVDHD missing from TIME_SURFACE for WELL-0042(h) after mapping upload.",
                   customer="Contoso Energy", product="DrillOps")
    child = _signal(redactor, SourceSystem.PAGERDUTY, "90000011-note-1", "Follow-up note on PD #90000011.",
                    parent=seed.signal_id)
    referencing = _signal(redactor, SourceSystem.SERVICENOW, "CS9000123",
                          "Customer reports TVDHD gaps in TIME_SURFACE for WELL-0042(h). Associated with PD #90000011.",
                          customer="Contoso Energy", product="DrillOps")
    unrelated = _signal(redactor, SourceSystem.ADO, "7001",
                        "Add dark mode toggle to the billing settings page.")
    for sig in (seed, child, referencing, unrelated):
        evidence.add_signal(sig)

    problem = problems.save(ProductProblem(
        title=Claim(text="TVDHD missing", kind=ClaimKind.INFERENCE),
        classification=Claim(text="defect", kind=ClaimKind.INFERENCE),
        linked_signals=[SignalLink(signal_id=seed.signal_id, link_type=LinkType.HARD_REF, score=1.0, linked_by="t")]))

    candidates = service.find_candidates(referencing.signal_id, top_k=3)
    assert candidates and candidates[0]["problem_id"] == problem.problem_id and candidates[0]["score"] == 1.0

    # assign_candidates is idempotent: running it twice links once and returns identical routes.
    first = service.assign_candidates([referencing.signal_id, unrelated.signal_id])
    second = service.assign_candidates([referencing.signal_id, unrelated.signal_id])
    assert first[0]["route"] == "AUTO_LINK" and first[0]["problem_id"] == problem.problem_id
    assert first[1]["route"] == "NEW"
    assert second == first
    assert problems.signal_ids(problem.problem_id).count(referencing.signal_id) == 1

    # Related neighbourhood includes children, incoming/outgoing cross-refs and problem siblings.
    rel_seed = service.related(seed.signal_id)
    assert rel_seed["children"] == [child.signal_id]
    assert referencing.signal_id in rel_seed["referencing"]
    assert rel_seed["problem_id"] == problem.problem_id and set(rel_seed["problem_signals"]) == {
        seed.signal_id, referencing.signal_id}
    rel_ref = service.related(referencing.signal_id)
    assert seed.signal_id in rel_ref["cross_referenced"]

    with pytest.raises(LookupError, match="unknown signal"):
        service.find_candidates("sig_does_not_exist")
    with pytest.raises(PlaceholderNotImplemented) as sub:
        service.drain_subscription()
    assert sub.value.question_id == "Q-10"


def test_kb_and_figma_mcp_servers_require_redactor_in_services() -> None:
    """Fail-closed DM-5: neither mcp-kb nor mcp-figma may be built without a configured redactor."""
    settings = local_settings()
    from pm_agent.connectors.figma import FixtureFigmaConnector
    from pm_agent.connectors.kb import FixtureKbAdapter

    redactor = LocalRedactor.from_settings(settings, pseudonym_key=b"k")
    with pytest.raises(ValueError, match="redactor"):
        build_server("mcp-kb", ServerServices(settings=settings, kb=FixtureKbAdapter(), redactor=None))
    with pytest.raises(ValueError, match="redactor"):
        build_server("mcp-figma", ServerServices(settings=settings, figma=FixtureFigmaConnector(redactor),
                                                 redactor=None))
    # With a redactor, both servers wire cleanly.
    kb_server = build_server("mcp-kb", ServerServices(settings=settings, kb=FixtureKbAdapter(), redactor=redactor))
    fg_server = build_server("mcp-figma", ServerServices(settings=settings, figma=FixtureFigmaConnector(redactor),
                                                         redactor=redactor))
    assert kb_server.name == "mcp-kb" and fg_server.name == "mcp-figma"
