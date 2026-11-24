"""Offline models for tests and credential-free demos (never used in stage/prod).

* ``ScriptedLlm`` / ``ScriptedModelProvider`` — exact canned JSON per node (component tests control precisely
  which claims are cited, uncited or hallucinated).
* ``ExtractiveLlm`` / ``ExtractiveModelProvider`` — a deterministic *extractive* drafter: every claim is a sentence
  copied verbatim from the evidence pack with that sentence as its quote, chosen by keyword heuristics. It cannot
  hallucinate, which makes it a useful local baseline for evals and demos — but it is not a product feature.
"""

from __future__ import annotations

import json
import re
from collections.abc import AsyncGenerator, Callable
from typing import Any

from google.adk.models.base_llm import BaseLlm
from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse
from google.genai import types

from pm_agent.agents.factory import Role

_SENTENCE = re.compile(r"[^.!?\n]+[.!?]?")
CHECKLIST_KEYWORDS: dict[str, tuple[str, ...]] = {
    "who_affected": ("customer", "user", "operator", "engineer", "affected", "reported", "team"),
    "current_vs_desired_workflow": ("currently", "today", "instead", "expected", "workflow", "manual", "should"),
    "should_do_should_not": ("must", "should", "need", "require", "never", "only"),
    "acceptance_criteria": ("acceptance", "criteria", "verify", "within", "success", "done when"),
    "edge_cases": ("edge", "unless", "except", "late", "missing", "fails", "error"),
    "dependencies_constraints": ("depend", "constraint", "requires", "version", "after", "before", "api"),
}
FIELD_KEYWORDS: dict[str, tuple[str, ...]] = {
    "summary": ("not", "fail", "request", "need", "issue", "problem"),
    "user_workflow": ("workflow", "when", "user", "operator", "upload", "stream"),
    "desired_outcome": ("should", "expected", "want", "need", "request"),
    "in_scope": ("must", "should", "need"),
    "out_of_scope": ("out of scope", "not required", "later"),
    "acceptance_criteria": CHECKLIST_KEYWORDS["acceptance_criteria"],
    "edge_cases": CHECKLIST_KEYWORDS["edge_cases"],
    "dependencies": ("depend", "requires", "after", "before"),
    "technical_constraints": ("version", "api", "witsml", "server", "limit", "mapping"),
}
INTENT_KEYWORDS: list[tuple[str, tuple[str, ...]]] = [
    ("define_requirement", ("requirement", "draft", "prd", "spec")),
    ("design_prototype", ("prototype", "figma", "mockup", "design")),
    ("investigate_issue", ("investigate", "incident", "root cause", "outage")),
    ("capture_knowledge", ("knowledge", "kb", "article")),
    ("trace_roadmap", ("roadmap", "trace", "lineage")),
    ("surface_trends", ("trend", "recurring", "theme")),
    ("review_problems", ("problems", "inbox", "triage")),
    ("build_problem", ("correlate", "cluster", "build problem")),
]


class ScriptedLlm(BaseLlm):  # pylint: disable=abstract-method  # no live (bidi) mode offline
    """Returns canned responses in order (the last one repeats). ``calls`` records every request's text.

    A response may be a callable ``(request_text) -> dict | str`` for outputs that must cite ids created at runtime.
    """

    responses: list[Any] = []
    calls: list[str] = []

    async def generate_content_async(self, llm_request: LlmRequest,
                                     stream: bool = False) -> AsyncGenerator[LlmResponse, None]:
        del stream
        request = _request_text(llm_request)
        self.calls.append(request)
        index = min(len(self.calls), len(self.responses)) - 1
        reply = self.responses[index] if self.responses else {}
        if callable(reply):
            reply = reply(request)  # pylint: disable=not-callable
        text = reply if isinstance(reply, str) else json.dumps(reply)
        yield LlmResponse(content=types.Content(role="model", parts=[types.Part(text=text)]))


class ScriptedModelProvider:
    """One ``ScriptedLlm`` per node name; nodes without a script raise so tests notice unexpected calls."""

    def __init__(self, script: dict[str, list[Any]]) -> None:
        self.models = {name: ScriptedLlm(model=f"scripted/{name}", responses=list(replies), calls=[])
                       for name, replies in script.items()}

    def model_for(self, node_name: str, role: Role) -> str | BaseLlm:
        del role
        if node_name not in self.models:
            raise KeyError(f"no script for LLM node {node_name!r}")
        return self.models[node_name]


def _request_text(llm_request: LlmRequest) -> str:
    return "\n".join(part.text or "" for content in llm_request.contents for part in (content.parts or []))


def _sentences(item: dict[str, Any]) -> list[str]:
    return [s.strip() for s in _SENTENCE.findall(str(item.get("text", ""))) if len(s.strip()) > 12]


def _claim(item: dict[str, Any], sentence: str, kind: str = "fact") -> dict[str, Any]:
    return {"text": sentence, "kind": kind, "citations": [{"evidence_id": item["evidence_id"], "quote": sentence}]}


def _pick(evidence: list[dict[str, Any]], keywords: tuple[str, ...], limit: int = 2) -> list[dict[str, Any]]:
    claims: list[dict[str, Any]] = []
    for item in evidence:
        kind = "pm_judgment" if item.get("source_system") == "pm_input" else "fact"
        for sentence in _sentences(item):
            if any(k in sentence.lower() for k in keywords):
                claims.append(_claim(item, sentence, kind))
                if len(claims) >= limit:
                    return claims
    return claims


def _all_evidence(data: dict[str, Any]) -> list[dict[str, Any]]:
    return list(data.get("evidence", [])) + list(data.get("pm_answers", []))


def _readiness(data: dict[str, Any]) -> dict[str, Any]:
    items = []
    for name in data.get("checklist") or CHECKLIST_KEYWORDS:
        claims = _pick(_all_evidence(data), CHECKLIST_KEYWORDS.get(name, (name,)))
        items.append({"item": name, "status": "present" if claims else "missing", "claims": claims,
                      "gap": None if claims else f"No evidence yet for {name.replace('_', ' ')}."})
    return {"items": items}


def _questions(data: dict[str, Any]) -> dict[str, Any]:
    return {"questions": [{"checklist_item": gap, "why": [],
                           "text": f"Can you provide the {gap.replace('_', ' ')} for this problem?"}
                          for gap in data.get("gaps", [])]}


def _requirement(data: dict[str, Any]) -> dict[str, Any]:
    evidence = _all_evidence(data)
    return {"fields": [{"name": name, "claims": _pick(evidence, FIELD_KEYWORDS.get(name, (name,)), limit=2)}
                       for name in data.get("template_fields", [])]}


def _feasibility(data: dict[str, Any]) -> dict[str, Any]:
    claims = _pick(_all_evidence(data), FIELD_KEYWORDS["technical_constraints"])
    return {"verdict": "constraints" if claims else "supportable", "claims": claims, "affected_components": []}


def _problem(data: dict[str, Any]) -> dict[str, Any]:
    evidence = _all_evidence(data)
    first = next(((item, s) for item in evidence for s in _sentences(item)), None)
    if first is None:
        raise ValueError("no evidence to characterize")
    claim = _claim(*first)
    text = claim["text"].lower()
    problem_class = "feature_request" if any(k in text for k in ("request", "would like", "need")) else "defect"
    return {"title": claim, "classification": dict(claim, kind="classification"), "problem_class": problem_class,
            "confidence": "medium", "impact_business": [], "impact_customer": _pick(evidence, ("customer",), 1)}


def _answer(data: dict[str, Any]) -> dict[str, Any]:
    words = tuple(w for w in re.findall(r"[a-z0-9]{4,}", str(data.get("question", "")).lower()))
    claims = _pick(_all_evidence(data), words or ("problem",), limit=3)
    return {"claims": claims or [{"text": "The available evidence does not answer this question.",
                                  "kind": "inference", "citations": []}]}


def _intent(data: dict[str, Any] | str) -> dict[str, Any]:
    text = json.dumps(data).lower()
    capability = next((cap for cap, keys in INTENT_KEYWORDS if any(k in text for k in keys)), "ask")
    problem = re.search(r"prb_[0-9a-z]+", text, re.IGNORECASE)
    return {"capability": capability, "problem_id": problem.group(0) if problem else None, "question": None}


_HANDLERS: dict[str, Callable[[Any], dict[str, Any]]] = {
    "intent_router": _intent, "readiness_assessor": _readiness, "question_generator": _questions,
    "spec_writer": _requirement, "feasibility_assessor": _feasibility, "problem_characterizer": _problem,
    "answer_writer": _answer,
}


class ExtractiveLlm(BaseLlm):  # pylint: disable=abstract-method
    """Deterministic extractive drafter for one node (see module docstring)."""

    node: str = ""

    async def generate_content_async(self, llm_request: LlmRequest,
                                     stream: bool = False) -> AsyncGenerator[LlmResponse, None]:
        del stream
        raw = _request_text(llm_request)
        try:
            data: Any = json.loads(raw)
        except json.JSONDecodeError:
            data = {"request": raw}
        handler = _HANDLERS.get(self.node)
        if handler is None:
            raise NotImplementedError(f"no offline handler for {self.node}")
        yield LlmResponse(content=types.Content(role="model", parts=[types.Part(text=json.dumps(handler(data)))]))


class ExtractiveModelProvider:
    def model_for(self, node_name: str, role: Role) -> str | BaseLlm:
        del role
        return ExtractiveLlm(model=f"offline/{node_name}", node=node_name)
