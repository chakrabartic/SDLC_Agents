"""Synthetic golden fixtures for local runs, demos and tests (``tests/fixtures/connectors``).

``golden_connectors(redactor)`` returns one ``SourceConnector`` per golden source — ADO, ServiceNow, PagerDuty, Email
and OneDrive — over a coherent scenario (problems P1 and P2, one exact duplicate, one cross-system near-duplicate,
secrets and PII in free text, deliberate readiness gaps) documented in ``golden_manifest.json``. The W1 ingestion
workflow polls these exactly like the real connectors.

Fixtures live in the repository (not in images): ``FIXTURE_ROOT`` honours ``$PM_AGENT_FIXTURE_ROOT``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pm_agent.connectors.ado import FixtureAdoConnector
from pm_agent.connectors.base import SourceConnector
from pm_agent.connectors.common import default_fixture_root
from pm_agent.connectors.entities import EntityResolver
from pm_agent.connectors.figma import FixtureFigmaConnector
from pm_agent.connectors.kb import FixtureKbAdapter
from pm_agent.connectors.msgraph import FixtureGraphDriveConnector, FixtureGraphMailConnector
from pm_agent.connectors.pagerduty import FixturePagerDutyConnector
from pm_agent.connectors.servicenow import FixtureServiceNowConnector
from pm_agent.grounding.redaction import Redactor
from pm_agent.schemas import SourceSystem
from pm_agent.settings import Settings, load_settings

FIXTURE_ROOT: Path = default_fixture_root()
MANIFEST = "golden_manifest.json"
GOLDEN_SOURCES = (SourceSystem.ADO, SourceSystem.SERVICENOW, SourceSystem.PAGERDUTY, SourceSystem.EMAIL,
                  SourceSystem.ONEDRIVE)


def _root(root: Path | None) -> Path:
    base = root or FIXTURE_ROOT
    if not (base / MANIFEST).is_file():
        raise FileNotFoundError(f"golden connector fixtures not found under {base} "
                                "(dev checkouts only; set PM_AGENT_FIXTURE_ROOT to override)")
    return base


@dataclass(frozen=True)
class GoldenFixtures:
    """The golden fixture connectors with their concrete types (read MCP servers need more than ``poll``)."""

    ado: FixtureAdoConnector
    servicenow: FixtureServiceNowConnector
    pagerduty: FixturePagerDutyConnector
    mail: FixtureGraphMailConnector
    drive: FixtureGraphDriveConnector

    def by_source(self) -> dict[SourceSystem, SourceConnector]:
        return {SourceSystem.ADO: self.ado, SourceSystem.SERVICENOW: self.servicenow,
                SourceSystem.PAGERDUTY: self.pagerduty, SourceSystem.EMAIL: self.mail,
                SourceSystem.ONEDRIVE: self.drive}


def load_golden_manifest(root: Path | None = None) -> dict[str, Any]:
    """The golden scenario description (problems, duplicates, secrets, hard links, readiness gaps)."""
    data: dict[str, Any] = json.loads((_root(root) / MANIFEST).read_text(encoding="utf-8"))
    return data


def golden_fixtures(redactor: Redactor, *, settings: Settings | None = None, entities: EntityResolver | None = None,
                    root: Path | None = None, run_id: str = "golden") -> GoldenFixtures:
    """Typed golden fixture connectors sharing the redactor and the config gazetteer."""
    base = _root(root)
    resolver = entities or EntityResolver.from_settings(settings or load_settings())
    return GoldenFixtures(
        ado=FixtureAdoConnector(redactor, path=base / "ado" / "workitems.json", entities=resolver, run_id=run_id),
        servicenow=FixtureServiceNowConnector(redactor, path=base / "servicenow" / "tables.json", entities=resolver,
                                              run_id=run_id),
        pagerduty=FixturePagerDutyConnector(redactor, path=base / "pagerduty" / "incidents.json", entities=resolver,
                                            run_id=run_id),
        mail=FixtureGraphMailConnector(redactor, path=base / "msgraph" / "mail.json", entities=resolver,
                                       run_id=run_id),
        drive=FixtureGraphDriveConnector(redactor, path=base / "msgraph" / "drive.json", entities=resolver,
                                         run_id=run_id))


def golden_connectors(redactor: Redactor, *, settings: Settings | None = None,
                      entities: EntityResolver | None = None, root: Path | None = None,
                      run_id: str = "golden") -> dict[SourceSystem, SourceConnector]:
    """One fixture connector per golden source (ADO, ServiceNow, PagerDuty, Email, OneDrive) for W1 to poll."""
    return golden_fixtures(redactor, settings=settings, entities=entities, root=root, run_id=run_id).by_source()


def fixture_figma_connector(redactor: Redactor, *, root: Path | None = None,
                            run_id: str = "fixture") -> FixtureFigmaConnector:
    return FixtureFigmaConnector(redactor, path=_root(root) / "figma" / "library.json", run_id=run_id)


def fixture_kb_adapter(*, root: Path | None = None, approval_workflow: bool = False) -> FixtureKbAdapter:
    return FixtureKbAdapter(_root(root) / "kb" / "articles.json", approval_workflow=approval_workflow)
