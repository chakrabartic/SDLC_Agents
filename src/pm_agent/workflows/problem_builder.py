"""W2 — Problem builder (docs/05 §4.2): link signals to Product Problems, characterize, aggregate, verify.

``score_signals`` (code) → ``apply_links`` (code) → per touched problem: ``problem_characterizer`` (LLM) →
``upsert_problem`` (code: Verifier V1–V5 on every claim, aggregates recomputed by V6, deterministic fallbacks when
the model's title/classification is blocked). Grey-zone links are kept with ``LinkType.SEMANTIC`` and listed for PM
confirmation (requirements §13) — the cited LLM adjudicator is a later increment.
"""

from __future__ import annotations

from typing import Any

from google.adk import Context
from google.adk.workflow import node

from pm_agent.agents.factory import LlmNodes
from pm_agent.agents.llm_schemas import ProblemOut, to_claim_draft, to_claim_drafts
from pm_agent.correlation.aggregates import first_sentence
from pm_agent.runtime import Services
from pm_agent.schemas import (
    CitedQuote,
    Claim,
    ClaimDraft,
    ClaimKind,
    Confidence,
    LinkType,
    ProblemStatus,
    ProductProblem,
    SignalLink,
)
from pm_agent.workflows.common import dump_blocked, signals_pack

PENDING_CLASSIFICATION = "Classification pending PM review."


def build_problem_builder(services: Services, llm: LlmNodes) -> Any:
    """Return the ``problem_builder`` dynamic node (input: ``{"signal_ids": [...], "twins": {...}}``)."""

    def score_signals(node_input: dict[str, Any]) -> dict[str, Any]:
        signals = [s for s in (services.evidence.get_signal(i) for i in node_input.get("signal_ids", [])) if s]
        assignment = services.correlation.assign_batch(signals, dict(node_input.get("twins") or {}))
        return {"decisions": assignment.decisions, "merge_candidates": assignment.merge_candidates}

    def apply_links(node_input: dict[str, Any]) -> dict[str, Any]:
        links: dict[str, list[dict[str, Any]]] = {}
        for decision in node_input["decisions"]:
            links.setdefault(decision["problem_id"], []).append(decision)
        return {"touched": sorted(links), "links": links,
                "needs_confirmation": [d["signal_id"] for d in node_input["decisions"] if d["route"] == "ADJUDICATE"],
                "merge_candidates": node_input.get("merge_candidates", [])}

    def characterization_pack(node_input: dict[str, Any]) -> dict[str, Any]:
        problem_id = node_input["problem_id"]
        existing = services.problems.signal_ids(problem_id) if _exists(problem_id) else []
        new_ids = [d["signal_id"] for d in node_input["links"]]
        return {"task": "characterize the consolidated product problem", "problem_id": problem_id,
                **signals_pack(services, existing + new_ids)}

    def _exists(problem_id: str) -> bool:
        try:
            services.problems.get(problem_id)
        except LookupError:
            return False
        return True

    def _fallback_title(signal_ids: list[str]) -> ClaimDraft:
        for signal_id in signal_ids:
            chunks = services.evidence.chunks_for(signal_id)
            if chunks:
                sentence = first_sentence(chunks[0].text)
                return ClaimDraft(text=sentence, kind=ClaimKind.FACT,
                                  citations=[CitedQuote(evidence_id=chunks[0].evidence_id, quote=sentence)])
        raise LookupError("problem has no evidence")

    def upsert_problem(node_input: dict[str, Any]) -> dict[str, Any]:
        problem_id = node_input["problem_id"]
        decisions = node_input["links"]
        raw = ProblemOut.model_validate(node_input["raw"])
        existing = services.problems.get(problem_id) if _exists(problem_id) else None
        signal_ids = (services.problems.signal_ids(problem_id) if existing else []) + [d["signal_id"]
                                                                                       for d in decisions]
        verifier, blocked = services.verifier, []
        title_check = verifier.check_claims([to_claim_draft(raw.title)])
        blocked += title_check.blocked
        title = title_check.passed or verifier.check_claims([_fallback_title(signal_ids)]).passed
        class_check = verifier.check_claims([to_claim_draft(raw.classification)])
        blocked += class_check.blocked
        classification = class_check.passed[0] if class_check.passed else Claim(
            text=PENDING_CLASSIFICATION, kind=ClaimKind.INFERENCE)
        impact_b = verifier.check_claims(to_claim_drafts(raw.impact_business))
        impact_c = verifier.check_claims(to_claim_drafts(raw.impact_customer))
        blocked += impact_b.blocked + impact_c.blocked
        signals = [s for s in (services.evidence.get_signal(i) for i in signal_ids) if s]
        aggregates = [d for d in (services.aggregates.customers(signals, services.organizations),
                                  services.aggregates.frequency(signals)) if d]
        checked_aggregates = verifier.check_claims(aggregates)
        blocked += checked_aggregates.blocked
        affected = next((c for c in checked_aggregates.passed if c.text.startswith("Reported by")), None)
        frequency = next((c for c in checked_aggregates.passed if "linked signal" in c.text), None)
        links = list(existing.linked_signals) if existing else []
        links += [SignalLink(signal_id=d["signal_id"], link_type=LinkType(d["link_type"]),
                             score=min(max(float(d["score"]), 0.0), 1.0), linked_by="problem_builder")
                  for d in decisions if all(link.signal_id != d["signal_id"] for link in links)]
        confident = class_check.passed and raw.confidence != "low"
        problem = ProductProblem(
            problem_id=problem_id, version=existing.version if existing else 1, title=title[0],
            classification=classification,
            classification_confidence=Confidence(raw.confidence) if confident else Confidence.LOW,
            linked_signals=links,
            affected_customers=affected, request_frequency=frequency,
            impact_business=impact_b.passed, impact_customer=impact_c.passed,
            readiness=existing.readiness if existing else None,
            open_questions=existing.open_questions if existing else [],
            status=existing.status if existing else ProblemStatus.NEW)
        services.problems.save(problem)
        return {"problem_id": problem_id, "title": problem.title.text, "created": existing is None,
                "signals": len(links), "blocked": dump_blocked(blocked)}

    @node(rerun_on_resume=True, name="problem_builder")
    async def problem_builder(ctx: Context, node_input: Any) -> dict[str, Any]:
        request = node_input if isinstance(node_input, dict) else {}
        decisions = await ctx.run_node(score_signals, request)
        applied = await ctx.run_node(apply_links, decisions)
        results = []
        for problem_id in applied["touched"]:
            links = applied["links"][problem_id]
            pack = await ctx.run_node(characterization_pack, {"problem_id": problem_id, "links": links})
            raw = await ctx.run_node(llm["problem_characterizer"], pack)
            results.append(await ctx.run_node(upsert_problem, {"problem_id": problem_id, "links": links,
                                                               "raw": raw}))
        return {"status": "ok", "problems": results, "needs_confirmation": applied["needs_confirmation"],
                "merge_candidates": applied["merge_candidates"], "summary": request.get("summary")}

    return problem_builder
