"""``pm_agent_core`` Agent Runtime app. ``app`` is built lazily for the deployment loader."""

from typing import Any

from pm_agent.core_app.agent import APP_NAME, build_core_app, build_core_workflow

__all__ = ["APP_NAME", "build_core_app", "build_core_workflow"]


def __getattr__(name: str) -> Any:
    if name == "app":
        from pm_agent.agents.factory import SettingsModelProvider  # pylint: disable=import-outside-toplevel
        from pm_agent.runtime import build_services  # pylint: disable=import-outside-toplevel

        services = build_services()
        return build_core_app(services, SettingsModelProvider(services.settings))
    raise AttributeError(name)
