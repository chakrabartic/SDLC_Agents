"""W1 ingestion + W2 problem builder through the ADK Runner (offline extractive model, in-test connectors)."""
from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from pm_agent.agents.offline import ExtractiveModelProvider
from pm_agent.connectors.base import ConnectorError, PollResult, make_signal
from pm_agent.grounding import LocalRedactor
from pm_agent.ingestion_app import build_ingestion_app
from pm_agent.placeholders import not_implemented
from pm_agent.runtime import build_local_services
from pm_agent.workflows.ask import build_review_problems
from pm_agent.schemas import (
    ClaimKind,
    DuplicateCandidate,
    DuplicateDecision,
    PersonRef,
    ProblemStatus,
    RecordType,
    SourceSystem,
    VerificationOutcome,
)
from tests.component.helpers import AppSession
from tests.support import local_settings

pytestmark = pytest.mark.component

PD_BODY = ("Standardized log TIME_SURFACE is missing channel TVDHD for well WELL-0042(h) at CNE. "
           "The mapping file was uploaded after the channel started streaming. "
           "Contact jane.doe@example.com, connection Server=db;Password=hunter2.")
SNOW_BODY = ("Customer Fabrikam Petroleum reports TVDHD gaps in TIME_SURFACE. Associated with PD #90000011. "
             "Engineering bug: https://dev.azure.com/example/Proj/_workitems/edit/7000001")
EMAIL_BODY = ("Standardized log TIME_SURFACE is missing channel TVDHD for well WELL-0042(h) at CNE. "
              "The mapping file was uploaded after the channel started streaming. Please advise.")
ADO_BODY = ("Feature request: bulk export of depth logs to the rig WITSML store. Northwind Offshore needs "
            "a daily export of all depth logs without manual steps.")


class FakeConnector:
    def __init__(self, source: SourceSystem, signals: list, fail: bool = False,  # type: ignore[type-arg]
                 unconfigured: bool = False) -> None:
        self.source, self.signals, self.fail, self.unconfigured = source, signals, fail, unconfigured

    def poll(self, cursor: str | None, limit: int = 100) -> PollResult:
        del limit
        if self.fail:
            raise ConnectorError("unavailable", "sandbox down")
        if self.unconfigured:
            not_implemented("Q-13", "mailboxes in scope")
        start = int(cursor) if cursor else 0  # delta semantics: the cursor is the position after the last delivery
        return PollResult(source=self.source, signals=self.signals[start:], next_cursor=str(len(self.signals)))

    def get(self, record_id: str):  # type: ignore[no-untyped-def]
        return next((s for s in self.signals if s.source_record_id == record_id), None)


def _signal(redactor, system, record_id, record_type, text, day):  # type: ignore[no-untyped-def]
    return make_signal(redactor=redactor, source_system=system, source_record_id=record_id, source_version="1",
                       record_type=record_type, permalink=f"https://{system.value}.example.invalid/{record_id}",
                       raw_text=text, captured_utc=datetime(2026, 5, day, tzinfo=UTC), connector="fake")


def _connectors(settings):  # type: ignore[no-untyped-def]
    redactor = LocalRedactor.from_settings(settings, pseudonym_key=b"k")
    pd = _signal(redactor, SourceSystem.PAGERDUTY, "90000011", RecordType.INCIDENT, PD_BODY, 1)
    snow = _signal(redactor, SourceSystem.SERVICENOW, "CS0000456", RecordType.CASE, SNOW_BODY, 2)
    mail = _signal(redactor, SourceSystem.EMAIL, "msg-1", RecordType.EMAIL_MESSAGE, EMAIL_BODY, 3)
    ado = _signal(redactor, SourceSystem.ADO, "7000002", RecordType.WORK_ITEM, ADO_BODY, 4)
    return {SourceSystem.PAGERDUTY: FakeConnector(SourceSystem.PAGERDUTY, [pd]),
            SourceSystem.SERVICENOW: FakeConnector(SourceSystem.SERVICENOW, [snow]),
            SourceSystem.EMAIL: FakeConnector(SourceSystem.EMAIL, [mail]),
            SourceSystem.ADO: FakeConnector(SourceSystem.ADO, [ado, ado]),  # exact duplicate delivery
            SourceSystem.ONEDRIVE: FakeConnector(SourceSystem.ONEDRIVE, [], fail=True)}


async def test_ingestion_dedups_correlates_and_builds_verified_problems() -> None:
    settings = local_settings()
    services = build_local_services(settings, connectors=_connectors(settings))
    session = AppSession(build_ingestion_app(services, ExtractiveModelProvider()))
    turn = await session.send("ingest")
    summary = turn.output_of("emit_events")
    assert summary["stored"] == 4 and summary["dropped"] == 1
    assert summary["source_health"]["onedrive"] == "unavailable"  # partial data is loud
    problems = services.problems.list_problems()
    assert len(problems) == 2
    p1 = next(p for p in problems if len(p.linked_signals) == 3)
    linked_systems = {services.evidence.get_signal(link.signal_id).source_system for link in p1.linked_signals}
    assert linked_systems == {SourceSystem.PAGERDUTY, SourceSystem.SERVICENOW, SourceSystem.EMAIL}
    assert p1.affected_customers is not None and "2 customers" in p1.affected_customers.text
    assert p1.request_frequency is not None and p1.request_frequency.text.startswith("3 linked signals")
    for problem in problems:
        report = services.verifier.verify_subject(problem, subject_type="product_problem")
        assert report.overall is VerificationOutcome.PASS, report.checks
        assert problem.title.kind is ClaimKind.FACT and problem.title.citations
    stored_text = " ".join(s.raw_text for s in services.evidence.signals.values())
    assert "hunter2" not in stored_text and "jane.doe@example.com" not in stored_text


async def test_reingestion_is_idempotent() -> None:
    settings = local_settings()
    services = build_local_services(settings, connectors=_connectors(settings))
    app = build_ingestion_app(services, ExtractiveModelProvider())
    await AppSession(app).send("ingest")
    second = await AppSession(app).send("ingest")
    assert second.output_of("emit_events")["stored"] == 0
    assert len(services.problems.list_problems()) == 2


async def test_sources_awaiting_slb_details_are_reported_not_fatal() -> None:
    settings = local_settings()
    connectors = _connectors(settings)
    connectors[SourceSystem.ONEDRIVE] = FakeConnector(SourceSystem.ONEDRIVE, [], unconfigured=True)
    services = build_local_services(settings, connectors=connectors)
    turn = await AppSession(build_ingestion_app(services, ExtractiveModelProvider())).send("ingest")
    summary = turn.output_of("emit_events")
    assert summary["source_health"]["onedrive"] == "not_configured" and summary["stored"] == 4


async def test_reingestion_preserves_problem_fields_the_builder_does_not_own() -> None:
    """Re-upserting a problem patches what W2 owns (title, links, aggregates…) and keeps PM-owned state."""
    settings = local_settings()
    services = build_local_services(settings, connectors=_connectors(settings))
    await AppSession(build_ingestion_app(services, ExtractiveModelProvider())).send("ingest")
    p1 = next(p for p in services.problems.list_problems() if len(p.linked_signals) == 3)
    owner = PersonRef(token="PERSON_PM0001", role_hint="pm")
    duplicate = DuplicateCandidate(ref="ado:7000009", system="ado", similarity=0.91, method="shingle",
                                   decision=DuplicateDecision.DISTINCT)
    services.problems.save(p1.model_copy(update={"owner_pm": owner, "product_line": "DrillOps",
                                                 "duplicates": [duplicate], "status": ProblemStatus.GATHERING}))
    redactor = LocalRedactor.from_settings(settings, pseudonym_key=b"k")
    follow_up = _signal(redactor, SourceSystem.EMAIL, "msg-2", RecordType.EMAIL_MESSAGE, EMAIL_BODY, 5)
    services.connectors[SourceSystem.EMAIL].signals.append(follow_up)  # type: ignore[attr-defined]
    await AppSession(build_ingestion_app(services, ExtractiveModelProvider())).send("ingest")
    updated = services.problems.get(p1.problem_id)
    assert len(updated.linked_signals) == 4, "the follow-up mail joined the existing problem"
    assert updated.owner_pm == owner and updated.product_line == "DrillOps"
    assert updated.duplicates == [duplicate] and updated.status is ProblemStatus.GATHERING


class FailOnceStore:
    """Evidence store whose first ``add_signal`` fails (e.g. AlloyDB failover mid-window)."""

    def __init__(self, inner):  # type: ignore[no-untyped-def]
        self._inner, self.failures_left = inner, 1

    def add_signal(self, signal):  # type: ignore[no-untyped-def]
        if self.failures_left:
            self.failures_left -= 1
            raise ConnectionError("store unavailable")
        self._inner.add_signal(signal)

    def __getattr__(self, name):  # type: ignore[no-untyped-def]
        return getattr(self._inner, name)


async def test_a_failed_window_is_repolled_not_skipped() -> None:
    """Cursors advance only once the window is stored and linked (docs/09 §1); a failure re-polls the window."""
    settings = local_settings()
    services = build_local_services(settings, connectors=_connectors(settings))
    services.evidence = FailOnceStore(services.evidence)
    app = build_ingestion_app(services, ExtractiveModelProvider())
    with pytest.raises(ConnectionError):
        await AppSession(app).send("ingest")
    retry = await AppSession(app).send("ingest")
    assert retry.output_of("emit_events")["stored"] == 4
    assert len(services.problems.list_problems()) == 2
    assert services.ingestion.cursor("pagerduty") == "1" and services.ingestion.cursor("onedrive") is None


async def test_inbox_source_health_reflects_the_last_completed_poll() -> None:
    settings = local_settings()
    services = build_local_services(settings, connectors=_connectors(settings))
    await AppSession(build_ingestion_app(services, ExtractiveModelProvider())).send("ingest")
    assert services.ingestion.health() == {"ado": "ok", "email": "ok", "onedrive": "unavailable", "pagerduty": "ok",
                                           "servicenow": "ok"}
    inbox = build_review_problems(services)({})
    texts = json.dumps(inbox["a2ui"])
    assert "Partial data" in texts and "unavailable: onedrive" in texts
    assert "servicenow" not in texts.split("Partial data", 1)[1].split('"', 1)[0]
