"""The deployable ADK apps build lazily in a local checkout (Agent Runtime loads ``<module>.app``)."""
from __future__ import annotations

import pytest

pytestmark = pytest.mark.component


def test_deployable_apps_build_locally_without_credentials() -> None:
    import pm_agent.core_app as core  # pylint: disable=import-outside-toplevel
    import pm_agent.executor_app as executor  # pylint: disable=import-outside-toplevel
    import pm_agent.ingestion_app as ingestion  # pylint: disable=import-outside-toplevel

    assert core.app.name == "pm_agent_core"
    assert ingestion.app.name == "signal_ingestion"  # golden fixture connectors attached in a checkout
    assert executor.app.name == "commit_executor"
    with pytest.raises(AttributeError):
        getattr(core, "not_an_app")
