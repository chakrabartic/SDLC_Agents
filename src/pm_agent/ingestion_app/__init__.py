"""``signal_ingestion`` Agent Runtime app (W1 + W2). ``app`` is built lazily for the deployment loader."""

from typing import Any

from pm_agent.ingestion_app.agent import APP_NAME, build_ingestion_app, build_ingestion_workflow

__all__ = ["APP_NAME", "build_ingestion_app", "build_ingestion_workflow"]


def __getattr__(name: str) -> Any:
    if name == "app":
        from pm_agent.agents.factory import SettingsModelProvider  # pylint: disable=import-outside-toplevel
        from pm_agent.runtime import build_services  # pylint: disable=import-outside-toplevel

        services = build_services()
        return build_ingestion_app(services, SettingsModelProvider(services.settings))
    raise AttributeError(name)
