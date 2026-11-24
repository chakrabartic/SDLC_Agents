"""Renderer → agent ``action`` messages (A2UI v1.0 ``renderer_to_agent.json``) and PM decision events (docs/11 §6).

The renderer resolves each button's ``event.context`` bindings before sending, so everything in ``context`` is
**untrusted input**: the agent re-validates it, and decision actions additionally go Browser → BFF →
``mcp-ledger.decide`` with the PM's IAP identity — never through an LLM (H-2). Client ``@checks`` are UX only.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any, Final

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from pm_agent.a2ui._core import A2UI_VERSION
from pm_agent.schemas import Decision

APPROVE_ACTION: Final = "approve_action"
REJECT_ACTION: Final = "reject_action"
REQUEST_CHANGES: Final = "request_changes"
DECISION_ACTIONS: Final[frozenset[str]] = frozenset({APPROVE_ACTION, REJECT_ACTION, REQUEST_CHANGES})
_DECISIONS: Final[dict[str, Decision]] = {APPROVE_ACTION: Decision.APPROVE, REJECT_ACTION: Decision.REJECT,
                                          REQUEST_CHANGES: Decision.REQUEST_CHANGES}

# Ordinary events emitted by pm_agent.a2ui.surfaces (docs/11 §5); context keys are documented per builder.
EDIT_FIELD: Final = "edit_field"
ANSWER_QUESTIONS: Final = "answer_questions"
DRAFT_OUTREACH: Final = "draft_outreach"
OPEN_PROBLEM: Final = "open_problem"
FILTER: Final = "filter"
CONFIRM_MERGE: Final = "confirm_merge"
LINK_DUPLICATE: Final = "link_duplicate"
MARK_DISTINCT: Final = "mark_distinct"
SPLIT_SIGNAL: Final = "split_signal"
START_REQUIREMENT: Final = "start_requirement"
INVESTIGATE: Final = "investigate"
CONFIRM_OUTCOME: Final = "confirm_outcome"
DRAFT_KB: Final = "draft_kb"
DRAFT_BUG: Final = "draft_bug"
EXPORT: Final = "export"
SURFACE_EVENTS: Final[frozenset[str]] = DECISION_ACTIONS | frozenset({
    EDIT_FIELD, ANSWER_QUESTIONS, DRAFT_OUTREACH, OPEN_PROBLEM, FILTER, CONFIRM_MERGE, LINK_DUPLICATE, MARK_DISTINCT,
    SPLIT_SIGNAL, START_REQUIREMENT, INVESTIGATE, CONFIRM_OUTCOME, DRAFT_KB, DRAFT_BUG, EXPORT})

_SHA256: Final = re.compile(r"^sha256:[0-9a-f]{64}$")


class InvalidUiAction(ValueError):
    """Not a well-formed, safe-to-handle A2UI v1.0 renderer→agent ``action`` message."""


class UiAction(BaseModel):
    """A validated v1.0 ``action``: ``{name, surfaceId, sourceComponentId, timestamp, context, userMessage?}``."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    name: str = Field(min_length=1)
    surface_id: str = Field(alias="surfaceId", min_length=1)
    source_component_id: str = Field(alias="sourceComponentId", min_length=1)
    timestamp: AwareDatetime
    context: dict[str, Any]
    user_message: str | None = Field(default=None, alias="userMessage")
    metadata: dict[str, Any] | None = None

    @field_validator("timestamp", mode="before")
    @classmethod
    def _iso_string(cls, value: Any) -> Any:
        if not isinstance(value, str):
            raise ValueError("timestamp must be an ISO 8601 string")
        return value

    @model_validator(mode="after")
    def _decision_context(self) -> "UiAction":
        """Decision events must name the action they decide on; optional keys must have their documented types."""
        if self.name not in DECISION_ACTIONS:
            return self
        action_id = self.context.get("actionId")
        if not isinstance(action_id, str) or not action_id.strip():
            raise ValueError("decision actions must carry a non-empty string context.actionId")
        comment = self.context.get("comment")
        if comment is not None and not isinstance(comment, str):
            raise ValueError("context.comment must be a string")
        version = self.context.get("actionVersion")
        if version is not None and (isinstance(version, bool) or not isinstance(version, int)):
            raise ValueError("context.actionVersion must be an integer")
        digest = self.context.get("payloadSha256")
        if digest is not None and not (isinstance(digest, str) and _SHA256.match(digest)):
            raise ValueError("context.payloadSha256 must look like sha256:<64 hex>")
        return self

    @property
    def decision(self) -> Decision | None:
        """``approve`` / ``reject`` / ``request_changes`` for decision events, else ``None``."""
        return _DECISIONS.get(self.name)

    @property
    def action_id(self) -> str | None:
        value = self.context.get("actionId")
        return value if isinstance(value, str) else None

    @property
    def comment(self) -> str:
        value = self.context.get("comment")
        return value if isinstance(value, str) else ""


def parse_action(message: Mapping[str, Any]) -> UiAction:
    """Validate one renderer→agent v1.0 message and return its ``action``; raise ``InvalidUiAction`` otherwise.

    Accepts exactly ``{"version": "1.0", "action": {...}}`` (``renderer_to_agent.json``); ``callAgentFunction``,
    ``rendererFunctionResponse`` and ``error`` messages are refused here. Beyond the schema it requires an
    offset-aware ISO 8601 timestamp and, for decision events, a non-empty ``context.actionId``.
    """
    if not isinstance(message, Mapping):
        raise InvalidUiAction("an A2UI message must be a JSON object")
    if message.get("version") != A2UI_VERSION:
        raise InvalidUiAction(f"expected an A2UI {A2UI_VERSION} message")
    kinds = sorted(set(message) - {"version"})
    if kinds != ["action"]:
        raise InvalidUiAction(f"expected exactly one 'action' message, got {kinds}")
    payload = message["action"]
    if not isinstance(payload, Mapping):
        raise InvalidUiAction("'action' must be a JSON object")
    try:
        return UiAction.model_validate(dict(payload))
    except ValidationError as exc:
        reasons = "; ".join(f"{'.'.join(str(p) for p in err['loc']) or 'action'}: {err['msg']}"
                            for err in exc.errors(include_url=False, include_input=False))
        raise InvalidUiAction(f"invalid A2UI action: {reasons}") from exc


def is_decision(action: UiAction | str) -> bool:
    """True for ``approve_action`` / ``reject_action`` / ``request_changes`` (route to ``mcp-ledger.decide``)."""
    name = action.name if isinstance(action, UiAction) else action
    return name in DECISION_ACTIONS
