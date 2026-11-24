"""Correlation (docs/06 §6.1–6.2): batch assignment order, cluster merging, identifier-aware similarity."""
from __future__ import annotations

from datetime import UTC, datetime

import pytest

from pm_agent.connectors.base import make_signal
from pm_agent.correlation import CorrelationEngine, ProblemRepository
from pm_agent.correlation.scoring import LexicalScorer
from pm_agent.grounding import AtomExtractor, InMemoryEvidenceStore, LocalRedactor
from pm_agent.grounding.chunking import chunk_signal
from pm_agent.schemas import Claim, ClaimKind, LinkType, ProductProblem, RecordType, Signal, SignalLink, SourceSystem
from tests.support import local_settings


@pytest.fixture(name="world")
def _world():  # type: ignore[no-untyped-def]
    settings = local_settings()
    store, problems = InMemoryEvidenceStore(), ProblemRepository()
    engine = CorrelationEngine(settings, store, problems, LexicalScorer(settings, AtomExtractor.from_settings(settings)))
    return settings, store, problems, engine


def _add(store, system, record_id, text, day, hour=9, parent=None, record_type=RecordType.INCIDENT):  # type: ignore[no-untyped-def]
    signal = make_signal(redactor=LocalRedactor.from_settings(local_settings(), pseudonym_key=b"k"),
                         source_system=system, source_record_id=record_id, source_version="1",
                         record_type=record_type, permalink=f"https://{system.value}.example.invalid/{record_id}",
                         raw_text=text, captured_utc=datetime(2026, 5, day, hour, tzinfo=UTC),
                         parent_signal_id=parent)
    store.add_signal(signal)
    for chunk in chunk_signal(signal):
        store.add_chunk(chunk)
    return signal


def _problem_ids(assignment) -> dict[str, str]:  # type: ignore[no-untyped-def]
    return {d["signal_id"]: d["problem_id"] for d in assignment.decisions}


def test_children_join_their_parent_even_when_captured_earlier(world) -> None:  # type: ignore[no-untyped-def]
    _, store, _, engine = world
    parent = _add(store, SourceSystem.PAGERDUTY, "90000500", "Channel TVDHD missing from TIME_SURFACE.", 9, 12)
    child = _add(store, SourceSystem.PAGERDUTY, "PLOG1", "Triggered through the API.", 9, 10, parent=parent.signal_id,
                 record_type=RecordType.JOURNAL_NOTE)
    ids = _problem_ids(engine.assign_batch([child, parent]))
    assert ids[child.signal_id] == ids[parent.signal_id]


def test_a_signal_hard_linked_to_two_new_clusters_merges_them(world) -> None:  # type: ignore[no-untyped-def]
    _, store, _, engine = world
    mail = _add(store, SourceSystem.EMAIL, "m1", "Since this morning the TVDHD curve is empty for our wells.", 9)
    incident = _add(store, SourceSystem.PAGERDUTY, "90000501", "Disk usage crossed 85 percent on archive node.", 9, 11)
    bridge = _add(store, SourceSystem.ADO, "9100501", "Bug for PD #90000501 and the customer email thread.", 10)
    reply = _add(store, SourceSystem.EMAIL, "m2", "Tracked as Bug 9100501.", 10, 12, parent=mail.signal_id)
    assignment = engine.assign_batch([mail, incident, bridge, reply])
    ids = _problem_ids(assignment)
    assert len(set(ids.values())) == 1  # PD ← ADO bug ← reply → mail: one connected component
    assert assignment.new_problem_ids == [ids[mail.signal_id]]


def test_hard_links_across_existing_problems_need_a_pm_decision(world) -> None:  # type: ignore[no-untyped-def]
    _, store, problems, engine = world
    first = _add(store, SourceSystem.PAGERDUTY, "90000502", "TVDHD missing in TIME_SURFACE.", 1)
    second = _add(store, SourceSystem.ADO, "9100502", "Depth export to the rig store is manual.", 2,
                  record_type=RecordType.WORK_ITEM)
    for signal in (first, second):
        problems.save(ProductProblem(title=Claim(text="t", kind=ClaimKind.INFERENCE),
                                     classification=Claim(text="c", kind=ClaimKind.INFERENCE),
                                     linked_signals=[SignalLink(signal_id=signal.signal_id,
                                                                link_type=LinkType.HARD_REF, score=1.0,
                                                                linked_by="t")]))
    note = _add(store, SourceSystem.SERVICENOW, "CS0000777", "See PD #90000502 and Bug 9100502.", 3,
                record_type=RecordType.CASE)
    assignment = engine.assign_batch([note])
    assert assignment.merge_candidates and set(assignment.merge_candidates[0]["problems"]) == {
        problems.problem_for_signal(first.signal_id), problems.problem_for_signal(second.signal_id)}


def test_shared_domain_identifiers_link_terse_records(world) -> None:  # type: ignore[no-untyped-def]
    settings, store, _, engine = world
    scorer = LexicalScorer(settings, AtomExtractor.from_settings(settings))
    long_text = _add(store, SourceSystem.EMAIL, "m3", "For well WELL-0042(h) the TVDHD channel never reaches "
                                                      "TIME_SURFACE after the morning run at CNE.", 1)
    tracker = _add(store, SourceSystem.ONEDRIVE, "row-2", "Customer: CNE. Issue: TVDHD not populated in "
                                                          "TIME_SURFACE for WELL-0042(h).", 2,
                   record_type=RecordType.TRACKER_ROW)
    score, reason = scorer.score(long_text, tracker)
    assert score >= scorer.assign_threshold and "identifiers" in reason
    del engine


def test_customers_or_products_alone_never_link_problems(world) -> None:  # type: ignore[no-untyped-def]
    settings, store, _, _ = world
    scorer = LexicalScorer(settings, AtomExtractor.from_settings(settings))
    a = _add(store, SourceSystem.EMAIL, "m4", "CNE asks for a DrillOps WITSML dashboard.", 1)
    b = _add(store, SourceSystem.EMAIL, "m5", "CNE reports DrillOps WITSML login failures.", 2)
    score, _ = scorer.score(a, b)
    assert score < scorer.adjudicate_floor


def test_pseudonymized_people_are_not_identifiers(world) -> None:  # type: ignore[no-untyped-def]
    settings, store, _, _ = world
    scorer = LexicalScorer(settings, AtomExtractor.from_settings(settings))
    a: Signal = _add(store, SourceSystem.EMAIL, "m6", "Contact jane.doe@example.com about the export.", 1)
    b: Signal = _add(store, SourceSystem.EMAIL, "m7", "jane.doe@example.com says the login page is slow.", 2)
    assert scorer.identifiers(a.raw_text) == set() and scorer.score(a, b)[0] < scorer.adjudicate_floor
