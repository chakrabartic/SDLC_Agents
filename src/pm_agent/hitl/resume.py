"""Resuming a paused ADK workflow with the PM's decision (verified by probe SP-4, docs/02 §4).

A HITL node pauses by yielding ``RequestInput``; ADK emits an event whose function call is ``adk_request_input``
with ``id == interrupt_id`` and lists it in ``long_running_tool_ids``. The workbench resumes the *same invocation*
with a ``FunctionResponse`` carrying the outcome. The resume carries **no authority**: only the ledger can mint a
token, so a forged resume at worst makes the agent *ask* the executor, which then fails at ``consume_token``.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from google.genai import types

REQUEST_INPUT_FUNCTION = "adk_request_input"


@dataclass(frozen=True)
class PendingInterrupt:
    interrupt_id: str
    invocation_id: str | None
    message: str | None = None
    payload: dict[str, Any] = field(default_factory=dict)


def pending_interrupts(events: Iterable[Any]) -> list[PendingInterrupt]:
    """Interrupts that still wait for input, from ADK ``Event`` objects (duck-typed)."""
    found: dict[str, PendingInterrupt] = {}
    answered: set[str] = set()
    for event in events:
        content = getattr(event, "content", None)
        for part in getattr(content, "parts", None) or []:
            call, response = getattr(part, "function_call", None), getattr(part, "function_response", None)
            if call is not None and call.name == REQUEST_INPUT_FUNCTION and call.id:
                args = dict(call.args or {})
                payload = args.get("payload")
                found[call.id] = PendingInterrupt(interrupt_id=call.id,
                                                  invocation_id=getattr(event, "invocation_id", None),
                                                  message=args.get("message"),
                                                  payload=payload if isinstance(payload, dict) else {})
            if response is not None and response.name == REQUEST_INPUT_FUNCTION and response.id:
                answered.add(response.id)
    return [interrupt for key, interrupt in found.items() if key not in answered]


def resume_content(interrupt_id: str, response: dict[str, Any]) -> types.Content:
    """The ``new_message`` for ``Runner.run_async(..., invocation_id=<same>)``; *response* becomes the node output."""
    return types.Content(role="user", parts=[types.Part(function_response=types.FunctionResponse(
        id=interrupt_id, name=REQUEST_INPUT_FUNCTION, response=response))])


def decision_resume(interrupt_id: str, *, action_id: str, decision: str, outcome: str | None = None) -> types.Content:
    """Resume a HITL node with the ledger outcome of a PM decision (ids only — no content, no token)."""
    response: dict[str, Any] = {"action_id": action_id, "decision": decision}
    if outcome is not None:
        response["outcome"] = outcome
    return resume_content(interrupt_id, response)
