"""Run ADK apps in-process (no network, no LLM) and collect outputs and HITL interrupts."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types

from pm_agent.hitl.resume import PendingInterrupt, pending_interrupts


@dataclass
class Turn:
    events: list[Any] = field(default_factory=list)
    invocation_id: str | None = None

    @property
    def outputs(self) -> list[Any]:
        return [e.output for e in self.events if e.output is not None]

    def output_of(self, node_name: str) -> Any:
        """Latest output of the node whose path ends with ``/<node_name>@<n>``."""
        for event in reversed(self.events):
            path = str(getattr(event.node_info, "path", "") or "")
            if event.output is not None and path.rsplit("/", 1)[-1].split("@")[0] == node_name:
                return event.output
        return None

    @property
    def interrupts(self) -> list[PendingInterrupt]:
        return pending_interrupts(self.events)


class AppSession:
    """One user session against one ADK ``App``; supports resuming paused invocations."""

    def __init__(self, app: Any, user_id: str = "pm-1") -> None:
        self.service = InMemorySessionService()
        self.runner = Runner(app=app, session_service=self.service)
        self.app_name = app.name
        self.user_id = user_id
        self.session_id: str | None = None
        self.all_events: list[Any] = []

    async def send(self, message: types.Content | str | dict[str, Any], invocation_id: str | None = None) -> Turn:
        if self.session_id is None:
            session = await self.service.create_session(app_name=self.app_name, user_id=self.user_id)
            self.session_id = session.id
        if isinstance(message, dict):
            import json  # pylint: disable=import-outside-toplevel
            message = types.Content(role="user", parts=[types.Part(text=json.dumps(message))])
        elif isinstance(message, str):
            message = types.Content(role="user", parts=[types.Part(text=message)])
        turn = Turn()
        async for event in self.runner.run_async(user_id=self.user_id, session_id=self.session_id,
                                                 new_message=message, invocation_id=invocation_id):
            turn.events.append(event)
            turn.invocation_id = event.invocation_id or turn.invocation_id
        self.all_events.extend(turn.events)
        return turn
