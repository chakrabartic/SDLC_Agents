"""``commit_executor`` Agent Runtime app. ``app`` is built lazily for the deployment loader."""

from typing import Any

from pm_agent.executor_app.agent import APP_NAME, build_executor_app, build_executor_workflow

__all__ = ["APP_NAME", "build_executor_app", "build_executor_workflow"]


def __getattr__(name: str) -> Any:
    if name == "app":
        from pm_agent.runtime import LOCAL_EXECUTOR_ID, build_services  # pylint: disable=import-outside-toplevel

        return build_executor_app(build_services().executor, LOCAL_EXECUTOR_ID)
    raise AttributeError(name)
