"""Workbench BFF — the PM's entry point (docs/11 §2, §6; docs/07 §4).

* Chat and UI actions go to ``pm_agent_core`` (in-process locally; A2A through Agent Gateway ingress in
  production — placeholder until Agent Runtime endpoints exist, Q-10).
* **Decisions never transit an LLM**: decision events go to the ledger's ``decide`` with the PM's IAP assertion
  (after checking the action version and payload hash the PM saw), then the paused workflow is resumed with the
  outcome (ids only).
* A2UI messages are forwarded only when the client declared ``a2uiRendererCapabilities`` (text fallback otherwise);
  a surface id that is already live is deleted before it is re-created (v1.0 forbids duplicate ``createSurface``).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from fastapi import FastAPI, Header, HTTPException
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types
from pydantic import BaseModel, ConfigDict, Field

from pm_agent.a2ui import supports_a2ui
from pm_agent.a2ui.actions import (
    ANSWER_QUESTIONS,
    DRAFT_KB,
    INVESTIGATE,
    OPEN_PROBLEM,
    START_REQUIREMENT,
    InvalidUiAction,
    is_decision,
    parse_action,
)
from pm_agent.a2ui.surfaces import delete_surface
from pm_agent.agents.factory import ModelProvider, SettingsModelProvider
from pm_agent.core_app import build_core_app
from pm_agent.hitl.approvals import ApprovalError
from pm_agent.hitl.identity import IdentityError
from pm_agent.hitl.resume import decision_resume, pending_interrupts, resume_content
from pm_agent.runtime import LocalServices, build_services
from pm_agent.schemas import Approver, Decision

IAP_HEADER = "x-goog-iap-jwt-assertion"
UI_CAPABILITIES = {START_REQUIREMENT: "define_requirement", INVESTIGATE: "investigate_issue",
                   DRAFT_KB: "capture_knowledge", OPEN_PROBLEM: "review_problems"}


class _In(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    a2ui_renderer_capabilities: dict[str, Any] | None = Field(default=None, alias="a2uiRendererCapabilities")


class MessageIn(_In):
    text: str | None = None
    capability: str | None = None
    problem_id: str | None = None
    args: dict[str, Any] = Field(default_factory=dict)


class AnswerIn(_In):
    answers: list[dict[str, Any]] = Field(default_factory=list)
    decisions: list[dict[str, Any]] = Field(default_factory=list)
    proceed_with_gaps: bool = False
    reason: str = ""


class UiActionIn(_In):
    """An A2UI v1.0 renderer→agent message plus the renderer's capabilities (A2A message metadata)."""

    message: dict[str, Any]


class DecisionIn(_In):
    decision: Decision
    comment: str = ""
    interrupt_id: str | None = None
    action_version: int | None = Field(default=None, alias="actionVersion")
    payload_sha256: str | None = Field(default=None, alias="payloadSha256")


@dataclass
class _Pause:
    session_id: str
    invocation_id: str | None
    user_id: str
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass
class _State:
    sessions: dict[str, str] = field(default_factory=dict)
    pauses: dict[str, _Pause] = field(default_factory=dict)
    action_interrupts: dict[str, str] = field(default_factory=dict)
    live_surfaces: dict[str, set[str]] = field(default_factory=dict)


def create_app(services: LocalServices | None = None, provider: ModelProvider | None = None) -> FastAPI:  # pylint: disable=too-many-statements
    """The BFF over one in-process ``pm_agent_core`` runner (production: A2A to Agent Runtime)."""
    services = services or build_services()
    core = build_core_app(services, provider or SettingsModelProvider(services.settings))
    session_service = InMemorySessionService()  # production: Agent Platform Sessions behind the core app
    runner = Runner(app=core, session_service=session_service)
    state = _State()
    app = FastAPI(title="pm-workbench")

    def _pm(assertion: str | None) -> Approver:
        try:
            return services.identity.verify(assertion or "")
        except IdentityError as exc:
            raise HTTPException(status_code=401, detail="IAP assertion required") from exc

    async def _session(user_id: str) -> str:
        if user_id not in state.sessions:
            created = await session_service.create_session(app_name=core.name, user_id=user_id)
            state.sessions[user_id] = created.id
        return state.sessions[user_id]

    def _surfaces(session_id: str, messages: list[dict[str, Any]], capabilities: dict[str, Any] | None
                  ) -> list[dict[str, Any]]:
        if not supports_a2ui({"a2uiRendererCapabilities": capabilities} if capabilities else None):
            return []
        live = state.live_surfaces.setdefault(session_id, set())
        out: list[dict[str, Any]] = []
        for message in messages:
            created = (message.get("createSurface") or {}).get("surfaceId")
            deleted = (message.get("deleteSurface") or {}).get("surfaceId")
            if created and created in live:
                out.append(delete_surface(created))
            if created:
                live.add(created)
            if deleted:
                live.discard(deleted)
            out.append(message)
        return out

    async def _run(user_id: str, message: types.Content, invocation_id: str | None,
                   capabilities: dict[str, Any] | None) -> dict[str, Any]:
        session_id = await _session(user_id)
        events, current = [], invocation_id
        async for event in runner.run_async(user_id=user_id, session_id=session_id, new_message=message,
                                            invocation_id=invocation_id):
            events.append(event)
            current = event.invocation_id or current
        interrupts = []
        for pause in pending_interrupts(events):
            payload = {k: v for k, v in pause.payload.items() if k != "a2ui"}
            state.pauses[pause.interrupt_id] = _Pause(session_id=session_id, invocation_id=current, user_id=user_id,
                                                      payload=payload)
            if isinstance(payload.get("action_id"), str):
                state.action_interrupts[payload["action_id"]] = pause.interrupt_id
            interrupts.append({"interrupt_id": pause.interrupt_id, "message": pause.message, "payload": payload,
                               "a2ui": _surfaces(session_id, list(pause.payload.get("a2ui", [])), capabilities)})
        outputs = [e.output for e in events if e.output is not None]
        result = outputs[-1] if outputs and not interrupts else None
        a2ui: list[dict[str, Any]] = []
        if isinstance(result, dict):
            a2ui = _surfaces(session_id, list(result.get("a2ui", [])), capabilities)
            result = {k: v for k, v in result.items() if k != "a2ui"}
        return {"session_id": session_id, "invocation_id": current, "interrupts": interrupts, "result": result,
                "a2ui": a2ui}

    async def _decide(action_id: str, body: DecisionIn, assertion: str | None) -> dict[str, Any]:
        """Ledger first (identity, group, version, payload hash, freshness), then resume the paused workflow."""
        _pm(assertion)
        try:
            pending = services.approval_service.get_action(action_id)
        except ApprovalError as exc:
            raise HTTPException(status_code=404, detail="unknown action") from exc
        if body.payload_sha256 is not None and body.payload_sha256 != pending.payload_sha256:
            raise HTTPException(status_code=409, detail="payload_changed")  # approve exactly what was shown (H-4)
        try:
            outcome = services.approval_service.decide(action_id=action_id, decision=body.decision,
                                                assertion=assertion or "", comment=body.comment,
                                                expected_version=body.action_version)
        except ApprovalError as exc:
            raise HTTPException(status_code=409 if exc.code == "conflict" else 403, detail=exc.code) from exc
        response: dict[str, Any] = {"action_id": action_id, "status": outcome.action.status.value,
                                    "token_issued": outcome.token_issued, "approvals": outcome.approvals,
                                    "required": outcome.required}
        waiting = outcome.action.status.value == "PENDING_APPROVAL"  # two-person rule: more approvals needed
        interrupt_id = body.interrupt_id or state.action_interrupts.get(action_id)
        pause = state.pauses.get(interrupt_id or "")
        if pause is not None and not waiting and interrupt_id:
            state.pauses.pop(interrupt_id)
            response["workflow"] = await _run(pause.user_id, decision_resume(
                interrupt_id, action_id=action_id, decision=body.decision.value), pause.invocation_id,
                body.a2ui_renderer_capabilities)
        return response

    async def _answer(interrupt_id: str, body: AnswerIn, assertion: str | None) -> dict[str, Any]:
        pm = _pm(assertion)
        pause = state.pauses.get(interrupt_id)
        if pause is None or pause.user_id != pm.sub:
            raise HTTPException(status_code=404, detail="no such pending question")
        response = {"answers": body.answers, "decisions": body.decisions, "reason": body.reason,
                    "proceed_with_gaps": body.proceed_with_gaps, "assertion": assertion}
        state.pauses.pop(interrupt_id)
        return await _run(pm.sub, resume_content(interrupt_id, response), pause.invocation_id,
                          body.a2ui_renderer_capabilities)

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/api/messages")
    async def post_message(body: MessageIn, assertion: str | None = Header(default=None, alias=IAP_HEADER)
                           ) -> dict[str, Any]:
        pm = _pm(assertion)
        if body.capability:
            text = json.dumps({"capability": body.capability, "problem_id": body.problem_id, **body.args})
        else:
            text = body.text or ""
        return await _run(pm.sub, types.Content(role="user", parts=[types.Part(text=text)]), None,
                          body.a2ui_renderer_capabilities)

    @app.post("/api/interrupts/{interrupt_id}/answer")
    async def answer(interrupt_id: str, body: AnswerIn, assertion: str | None = Header(default=None, alias=IAP_HEADER)
                     ) -> dict[str, Any]:
        return await _answer(interrupt_id, body, assertion)

    @app.post("/api/actions/{action_id}/decide")
    async def decide(action_id: str, body: DecisionIn, assertion: str | None = Header(default=None, alias=IAP_HEADER)
                     ) -> dict[str, Any]:
        return await _decide(action_id, body, assertion)

    async def _answer_from_ui(pm: Approver, context: dict[str, Any], capabilities: dict[str, Any] | None,
                              assertion: str | None) -> dict[str, Any]:
        """``answer_questions`` resumes this PM's pending question pause for ``problemId``."""
        problem_id = context.get("problemId")
        interrupt_id = next((iid for iid, pause in state.pauses.items() if pause.user_id == pm.sub
                             and pause.payload.get("problem_id") == problem_id), None)
        if interrupt_id is None:
            raise HTTPException(status_code=404, detail="no pending questions for this problem")
        answers = [{"question_id": qid, "text": text} for qid, text in dict(context.get("answers") or {}).items()]
        return await _answer(interrupt_id, AnswerIn(answers=answers, a2uiRendererCapabilities=capabilities), assertion)

    @app.post("/api/ui-actions")
    async def ui_action(body: UiActionIn, assertion: str | None = Header(default=None, alias=IAP_HEADER)
                        ) -> dict[str, Any]:
        """A2UI v1.0 renderer→agent ``action`` messages: decisions → ledger; other events → capabilities."""
        pm = _pm(assertion)
        try:
            action = parse_action(body.message)
        except InvalidUiAction as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        capabilities = body.a2ui_renderer_capabilities
        context = action.context
        if is_decision(action) and action.decision is not None and action.action_id:
            return await _decide(action.action_id, DecisionIn(
                decision=action.decision, comment=action.comment, actionVersion=context.get("actionVersion"),
                payloadSha256=context.get("payloadSha256"), a2uiRendererCapabilities=capabilities),
                assertion)
        if action.name == ANSWER_QUESTIONS:
            return await _answer_from_ui(pm, context, capabilities, assertion)
        capability = UI_CAPABILITIES.get(action.name)
        if capability is None:
            raise HTTPException(status_code=422, detail=f"event {action.name} is not handled by this BFF yet")
        problem_ids = context.get("problemIds") or []
        problem_id = context.get("problemId") or (problem_ids[0] if problem_ids else None)
        text = json.dumps({"capability": capability, "problem_id": problem_id})
        return await _run(pm.sub, types.Content(role="user", parts=[types.Part(text=text)]), None, capabilities)

    @app.get("/api/problems")
    def problems(assertion: str | None = Header(default=None, alias=IAP_HEADER)) -> list[dict[str, Any]]:
        _pm(assertion)
        return [{"problem_id": p.problem_id, "title": p.title.text, "status": p.status.value,
                 "signals": len(p.linked_signals)} for p in services.problems.list_problems()]

    @app.get("/api/audit")
    def audit(action_id: str | None = None, problem_id: str | None = None,
              assertion: str | None = Header(default=None, alias=IAP_HEADER)) -> dict[str, Any]:
        _pm(assertion)
        entries = services.ledger.query(action_id=action_id, subject_type="product_problem" if problem_id else None,
                                        subject_id=problem_id)
        return {"chain_ok": services.ledger.verify_chain().ok,
                "entries": [e.model_dump(mode="json") for e in entries]}

    return app
