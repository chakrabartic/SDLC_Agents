"""Q&A and problem triage capabilities: ``ask`` (claim-structured answers) and ``review_problems`` (inbox)."""

from __future__ import annotations

from typing import Any

from google.adk import Context
from google.adk.workflow import node

from pm_agent.a2ui import surfaces as ui
from pm_agent.agents.factory import LlmNodes
from pm_agent.agents.llm_schemas import AnswerOut, to_claim_drafts
from pm_agent.ports import Services
from pm_agent.workflows.common import dump_blocked, dump_claims, evidence_item, request_text

UNVERIFIED_NOTE = "Some statements were removed because they could not be verified against the evidence."


def build_ask(services: Services, llm: LlmNodes) -> Any:
    """Retrieve evidence by code, draft with ``answer_writer``, return only verified claims (V1–V5)."""

    def retrieve(node_input: Any) -> dict[str, Any]:
        question = (node_input.get("question") if isinstance(node_input, dict) else None) or request_text(node_input)
        chunks = services.evidence.search(question, top_k=12)
        evidence = []
        for chunk in chunks:
            signal = services.evidence.get_signal(chunk.signal_id)
            if signal is not None:
                evidence.append(evidence_item(signal, chunk.evidence_id, chunk.text))
        return {"task": "answer", "question": question, "evidence": evidence, "pm_answers": []}

    def verify_answer(node_input: dict[str, Any]) -> dict[str, Any]:
        checked = services.verifier.check_claims(to_claim_drafts(AnswerOut.model_validate(node_input).claims))
        return {"status": "answered", "claims": dump_claims(checked.passed), "blocked": dump_blocked(checked.blocked),
                "note": UNVERIFIED_NOTE if checked.blocked else ""}

    @node(rerun_on_resume=True, name="ask")
    async def ask(ctx: Context, node_input: Any) -> dict[str, Any]:
        pack = await ctx.run_node(retrieve, node_input)
        if not pack["evidence"]:
            return {"status": "no_evidence", "claims": [], "blocked": [],
                    "note": "No evidence matches this question yet."}
        raw = await ctx.run_node(llm["answer_writer"], pack)
        answer: dict[str, Any] = await ctx.run_node(verify_answer, raw)
        return answer

    return ask


def build_review_problems(services: Services) -> Any:
    """Problem inbox: verified problem summaries plus per-source health (IR3), rendered as an A2UI surface."""

    def review_problems(node_input: Any) -> dict[str, Any]:
        del node_input
        problems = sorted(services.problems.list_problems(), key=lambda p: (-len(p.linked_signals), p.problem_id))
        health = services.ingestion.health()  # last completed poll per source; never-polled sources make no claim
        return {"status": "ok",
                "problems": [{"problem_id": p.problem_id, "title": p.title.text, "status": p.status.value,
                              "signals": len(p.linked_signals),
                              "customers": p.affected_customers.text if p.affected_customers else None}
                             for p in problems],
                "a2ui": ui.problem_inbox_surface(problems, health)}

    return review_problems
