"""``pm_agent_core`` — the PM Orchestrator app (docs/05 §3).

A dynamic workflow, not an LLM coordinator: one schema-bound ``intent_router`` call picks a capability from a closed
list, and a deterministic dispatch table runs the capability workflow as a checkpointed child. The supervisor is
loop-free, auditable and resumable across multi-day HITL pauses. LLM nodes hold **no tools**; GroundingGuard denies
any tool call they might attempt and records it in the ledger.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any

from google.adk import Context, Workflow
from google.adk.apps import App, ResumabilityConfig
from google.adk.plugins.base_plugin import BasePlugin
from google.adk.workflow import node

from pm_agent.agents.factory import NODE_SPECS, LlmNodes, ModelProvider
from pm_agent.grounding.plugin import DeniedToolCall, GroundingGuardPlugin
from pm_agent.ports import Services
from pm_agent.schemas import Actor, LedgerEntryType
from pm_agent.workflows.ask import build_ask, build_review_problems
from pm_agent.workflows.common import find_problem_id, request_text
from pm_agent.workflows.define_requirement import build_define_requirement
from pm_agent.workflows.knowledge import build_capture_knowledge
from pm_agent.workflows.operations import build_investigate_issue, build_surface_trends, build_trace_roadmap
from pm_agent.workflows.problem_builder import build_problem_builder
from pm_agent.workflows.prototype import build_design_prototype

APP_NAME = "pm_agent_core"
UNSUPPORTED = ("I can analyse signals and problems, draft requirements, prototypes and knowledge articles for your "
               "review, and trace evidence. I never approve, publish, send, buy or delete anything myself.")


def build_core_workflow(services: Services, provider: ModelProvider) -> Workflow:
    """Root workflow: ``START → pm_orchestrator`` with the capability dispatch table."""
    llm = LlmNodes(provider)
    capabilities: dict[str, Any] = {
        "ask": build_ask(services, llm),
        "review_problems": build_review_problems(services),
        "build_problem": build_problem_builder(services, llm),
        "define_requirement": build_define_requirement(services, llm),
        "design_prototype": build_design_prototype(services, llm),
        "investigate_issue": build_investigate_issue(services),
        "capture_knowledge": build_capture_knowledge(services, llm),
        "trace_roadmap": build_trace_roadmap(services),
        "surface_trends": build_surface_trends(services),
    }

    def parse_request(node_input: Any) -> dict[str, Any]:
        """Workbench UI actions arrive as JSON with an explicit capability; chat text goes to the router."""
        text = request_text(node_input)
        explicit: dict[str, Any] = {}
        if text.startswith("{"):
            try:
                loaded = json.loads(text)
                explicit = loaded if isinstance(loaded, dict) else {}
            except json.JSONDecodeError:
                explicit = {}
        capability = explicit.get("capability")
        return {"request": str(explicit.get("request") or text),
                "capability": capability if capability in capabilities else None,
                "problem_id": explicit.get("problem_id") or find_problem_id(text),
                "args": {k: v for k, v in explicit.items() if k not in {"capability", "request", "problem_id"}}}

    def unsupported(node_input: Any) -> dict[str, Any]:
        del node_input
        return {"status": "unsupported", "message": UNSUPPORTED}

    @node(rerun_on_resume=True, name="pm_orchestrator")
    async def pm_orchestrator(ctx: Context, node_input: Any) -> Any:
        request = await ctx.run_node(parse_request, node_input)
        intent = request if request["capability"] else await ctx.run_node(llm["intent_router"],
                                                                           {"request": request["request"]})
        workflow = capabilities.get(str(intent.get("capability")), unsupported)
        payload = {**request["args"], **{k: v for k, v in intent.items() if v is not None},
                   "request": request["request"], "problem_id": intent.get("problem_id") or request["problem_id"]}
        return await ctx.run_node(workflow, payload)

    return Workflow(name=APP_NAME, edges=[("START", pm_orchestrator)])


def build_core_app(services: Services, provider: ModelProvider, extra_plugins: Sequence[BasePlugin] = ()) -> App:
    """The deployable ``App``: orchestrator + GroundingGuard (+ Model Armor and logging plugins in production)."""

    def record_denial(denial: DeniedToolCall) -> None:
        services.ledger.record(LedgerEntryType.TOOL_CALL_DENIED, actor=Actor(type="agent", id=denial.agent or "?"),
                               payload={"agent": denial.agent, "tool": denial.tool, "arg_names": denial.arg_names})

    guard = GroundingGuardPlugin(allowed_tools={name: frozenset() for name in NODE_SPECS},
                                 write_tools=services.catalog.write_tools(), on_denied=record_denial)
    return App(name=APP_NAME, root_agent=build_core_workflow(services, provider), plugins=[guard, *extra_plugins],
               resumability_config=ResumabilityConfig(is_resumable=True))
