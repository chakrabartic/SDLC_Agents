"""LLM node factory — every model call is a single-turn, schema-bound ADK ``Agent`` node (docs/05 §6).

Models are resolved by role from configuration (pinned ids, ADR-008). Tests and offline demos inject a
``ModelProvider`` that returns ``BaseLlm`` instances instead (``pm_agent.agents.offline``), so the exact same
workflow graph runs with or without Gemini.
"""

from __future__ import annotations

from typing import Literal, Protocol

from google.adk import Agent
from google.adk.models.base_llm import BaseLlm
from google.adk.workflow import RetryConfig
from google.genai import types
from pydantic import BaseModel

from pm_agent.agents import instructions as ins
from pm_agent.agents import llm_schemas as out
from pm_agent.settings import Settings

Role = Literal["router", "drafter"]

NODE_SPECS: dict[str, tuple[Role, str, type[BaseModel]]] = {
    "intent_router": ("router", ins.INTENT_ROUTER, out.Intent),
    "readiness_assessor": ("drafter", ins.READINESS_ASSESSOR, out.ReadinessOut),
    "question_generator": ("drafter", ins.QUESTION_GENERATOR, out.QuestionsOut),
    "spec_writer": ("drafter", ins.SPEC_WRITER, out.RequirementOut),
    "feasibility_assessor": ("drafter", ins.FEASIBILITY_ASSESSOR, out.FeasibilityOut),
    "problem_characterizer": ("drafter", ins.PROBLEM_CHARACTERIZER, out.ProblemOut),
    "answer_writer": ("drafter", ins.ANSWER_WRITER, out.AnswerOut),
    "kb_writer": ("drafter", ins.KB_WRITER, out.KbArticleOut),
    "prototype_composer": ("drafter", ins.PROTOTYPE_COMPOSER, out.PrototypeOut),
}


class ModelProvider(Protocol):
    def model_for(self, node_name: str, role: Role) -> str | BaseLlm: ...


class SettingsModelProvider:
    """Production: pinned Gemini model ids from ``config.models`` (Gen AI SDK on Agent Platform via env)."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    def model_for(self, node_name: str, role: Role) -> str | BaseLlm:
        del node_name
        return self._settings.models.router if role == "router" else self._settings.models.drafter


class LlmNodes:
    """Builds (and caches) the ADK ``Agent`` for each named LLM node."""

    def __init__(self, provider: ModelProvider) -> None:
        self._provider = provider
        self._cache: dict[str, Agent] = {}

    def __getitem__(self, name: str) -> Agent:
        if name not in self._cache:
            role, instruction, schema = NODE_SPECS[name]
            self._cache[name] = Agent(
                name=name, model=self._provider.model_for(name, role), mode="single_turn",
                include_contents="none", instruction=instruction, output_schema=schema,
                retry_config=RetryConfig(max_attempts=2),
                generate_content_config=types.GenerateContentConfig(temperature=0.0 if role == "router" else 0.2))
        return self._cache[name]
