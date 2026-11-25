"""W6/K — Knowledge capture (docs/05 §4.6; FR-C4, AC6).

``load_resolution`` → ``kb_writer`` (LLM) → ``assemble_article`` (Verifier V1–V5 per section) → ``verify_knowledge``
(V0–V10 incl. near-duplicate KB articles and audience policy P-04) → ``propose`` KB_PUBLISH_ARTICLE
(product-kb-reviewers) → ⏸ → commit through the KB adapter (Q-01).
"""

from __future__ import annotations

from html import escape
from typing import Any

from google.adk import Context
from google.adk.workflow import node
from pydantic import BaseModel

from pm_agent.a2ui import surfaces as ui
from pm_agent.agents.factory import LlmNodes
from pm_agent.agents.llm_schemas import KbArticleOut, to_claim_draft, to_claim_drafts
from pm_agent.ports import Services
from pm_agent.schemas import ActionType, KbAudience, KnowledgeArticleDraft, SubjectRef
from pm_agent.workflows.approval import build_approval_nodes
from pm_agent.workflows.common import dump_blocked, problem_pack, resolve_problem, save_draft
from pm_agent.workflows.rendering import claims_html

KB = ActionType.KB_PUBLISH_ARTICLE.value
MAX_ATTEMPTS = 2


def render_kb_payload(article: BaseModel) -> dict[str, Any]:
    assert isinstance(article, KnowledgeArticleDraft)
    sections = [("Symptom", article.symptom), ("Root cause", article.root_cause),
                ("Resolution", article.resolution), ("Workaround", article.workaround)]
    body = "".join(f"<h3>{escape(title)}</h3>{claims_html(claims)}" for title, claims in sections if claims)
    return {"title": article.title.text, "audience": article.audience.value, "body_html": body,
            "applies_to_product": article.applies_to_product, "applies_to_versions": article.applies_to_versions,
            "labels": ["pm-agent"], "source_problem": article.derived_from_resolution}


def build_capture_knowledge(services: Services, llm: LlmNodes) -> Any:
    """Return the ``capture_knowledge`` dynamic node (input: ``{"problem_id", "audience"?}``)."""
    settings, verifier = services.settings, services.verifier

    def load_resolution(node_input: Any) -> dict[str, Any]:
        problem = resolve_problem(services, node_input, ask="Which resolved problem should become a knowledge article?")
        if isinstance(problem, dict):
            return problem
        data = node_input if isinstance(node_input, dict) else {}
        audience = str(data.get("audience") or KbAudience.INTERNAL.value)
        return {"problem_id": problem.problem_id, "audience": audience if audience in {a.value for a in KbAudience}
                else KbAudience.INTERNAL.value, "product": problem.product_line or settings.product.pilot_product_line,
                "pack": problem_pack(services, problem.problem_id)}

    def assemble_article(node_input: dict[str, Any]) -> dict[str, Any]:
        raw = KbArticleOut.model_validate(node_input["raw"])
        loaded = node_input["loaded"]
        title = verifier.check_claims([to_claim_draft(raw.title)])
        sections = {name: verifier.check_claims(to_claim_drafts(getattr(raw, name)))
                    for name in ("symptom", "root_cause", "resolution", "workaround")}
        blocked = title.blocked + [b for r in sections.values() for b in r.blocked]
        missing = [name for name in ("symptom", "root_cause", "resolution") if not sections[name].passed]
        if not title.passed:
            missing.insert(0, "title")
        if missing:
            return {"missing": missing, "blocked": dump_blocked(blocked)}
        article = KnowledgeArticleDraft(problem_id=loaded["problem_id"], title=title.passed[0],
                                        symptom=sections["symptom"].passed, root_cause=sections["root_cause"].passed,
                                        resolution=sections["resolution"].passed,
                                        workaround=sections["workaround"].passed,
                                        applies_to_product=loaded["product"], audience=KbAudience(loaded["audience"]),
                                        derived_from_resolution=loaded["problem_id"])
        ref = save_draft(services, "knowledge_article_draft", article,
                         related=[SubjectRef(type="product_problem", id=article.problem_id)],
                         payload={"blocked_claims": len(blocked)})
        return {"article_ref": ref, "missing": [], "blocked": dump_blocked(blocked)}

    def _target(subject: BaseModel) -> dict[str, Any]:
        assert isinstance(subject, KnowledgeArticleDraft)
        kb = settings.connectors.get("kb", {})
        return {"system": "kb", "backend": kb.get("backend"), "audience": subject.audience.value}

    def _surfaces(action: Any, subject: BaseModel, report: Any) -> list[dict[str, Any]]:
        assert isinstance(subject, KnowledgeArticleDraft)
        return ui.knowledge_review_surface(subject, report) + ui.approval_surface(action, subject, report)

    tail = build_approval_nodes(services, prefix="knowledge", action_type=KB, subject_type="knowledge_article_draft",
                                render=render_kb_payload, target=_target, surfaces=_surfaces,
                                message="Approval required (product KB reviewers): publish this article.")

    @node(rerun_on_resume=True, name="capture_knowledge")
    async def capture_knowledge(ctx: Context, node_input: Any) -> dict[str, Any]:
        loaded = await ctx.run_node(load_resolution, node_input)
        if loaded.get("error"):
            return {"status": loaded["error"], "message": loaded["message"]}
        assembled: dict[str, Any] = {}
        for _ in range(MAX_ATTEMPTS):
            raw = await ctx.run_node(llm["kb_writer"], {"task": "knowledge_article", "audience": loaded["audience"],
                                                         "previous_attempt_problems": assembled.get("missing", []),
                                                         **loaded["pack"]})
            assembled = await ctx.run_node(assemble_article, {"raw": raw, "loaded": loaded})
            if not assembled["missing"]:
                break
        else:
            return {"status": "needs_input", "missing": assembled["missing"], "blocked": assembled["blocked"]}
        verified = await ctx.run_node(tail.verify, {"subject_ref": assembled["article_ref"]})
        if verified["overall"] != "PASS":
            return {"status": "verification_" + verified["overall"].lower(), "problems": verified["failed"],
                    "article_ref": assembled["article_ref"]}
        outcome = await tail.run(ctx, verified)
        return {**outcome, "problem_id": loaded["problem_id"], "article_ref": assembled["article_ref"]}

    return capture_knowledge
