"""W4 — Prototype validation (docs/05 §4.4; FR-B1…FR-B6, AC4).

``guard_requirement`` (only from a committed, PM-approved requirement) → ``prototype_composer`` (LLM, DLS catalog
in the prompt, text by claim reference only) → ``build_spec`` → ``verify_prototype`` (V9: every component key in the
pinned DLS snapshot, reuse ratio, justified new patterns) → ``propose`` FIGMA_RENDER_PROTOTYPE (two-person rule:
PM + UX) → ⏸ → commit (executor → ``svc-prototype-renderer``, which compiles the Plugin API program).
"""

from __future__ import annotations

from typing import Any

from google.adk import Context
from google.adk.workflow import node
from pydantic import BaseModel

from pm_agent.a2ui import surfaces as ui
from pm_agent.agents.factory import LlmNodes
from pm_agent.agents.llm_schemas import PrototypeOut, SpecNodeOut, to_claim_drafts
from pm_agent.grounding.verifier import subject_digest
from pm_agent.prototype.dls import DlsCatalog, load_dls_catalog
from pm_agent.runtime import Services
from pm_agent.schemas import (
    ActionType,
    Actor,
    Claim,
    LedgerEntryType,
    NewPattern,
    PrototypeScreen,
    PrototypeSpec,
    RequirementDraft,
    SpecNode,
    SubjectRef,
    new_id,
)
from pm_agent.workflows.approval import build_approval_nodes
from pm_agent.workflows.common import dump_blocked, find_problem_id, problem_pack, request_text

FIGMA = ActionType.FIGMA_RENDER_PROTOTYPE.value
PROTOTYPE_FIELDS = ("summary", "user_workflow", "desired_outcome", "in_scope", "acceptance_criteria", "edge_cases")
MAX_ATTEMPTS = 3


def committed_requirement(services: Services, problem_id: str) -> RequirementDraft | None:
    """Latest requirement draft of *problem_id* whose ADO creation was approved and committed (ledger truth)."""
    latest: RequirementDraft | None = None
    for entry in services.ledger.query(entry_types=[LedgerEntryType.COMMIT_SUCCEEDED]):
        action = services.approvals.get_action(str(entry.payload.get("action_id")))
        if action.action_type is not ActionType.ADO_CREATE_WORK_ITEM:
            continue
        _, draft = services.subjects.get(action.draft_ref)
        if isinstance(draft, RequirementDraft) and draft.problem_id == problem_id:
            latest = draft
    return latest


def render_prototype_payload(spec: BaseModel) -> dict[str, Any]:
    """What the PM and UX reviewer approve; ``reuse_ratio``/``new_patterns`` also feed policy P-07."""
    assert isinstance(spec, PrototypeSpec)
    return {"spec_id": spec.spec_id, "spec_sha256": subject_digest(spec), "requirement_id": spec.requirement_id,
            "dls_library_file_key": spec.dls_library_file_key, "dls_published_version": spec.dls_published_version,
            "screens": [s.name for s in spec.screens], "component_keys": sorted(set(spec.component_keys())),
            "reuse_ratio": round(spec.reuse_ratio, 4),
            "new_patterns": [{"name": p.name, "justification": [c.text for c in p.justification]}
                             for p in spec.new_patterns]}


def _tree(nodes: list[SpecNodeOut], claim_ids: list[str]) -> list[SpecNode]:
    """Flat model output → SpecNode tree; text nodes keep only valid claim references (no new copy)."""
    built: list[SpecNode | None] = []
    for out in nodes:
        if out.kind == "text" and (out.text_claim is None or not 0 <= out.text_claim < len(claim_ids)):
            built.append(None)  # text without a verified claim is dropped, never invented
            continue
        built.append(SpecNode(kind=out.kind, name=out.name, component_key=out.component_key,
                              variant_props=out.variant_props,
                              text_claim_id=claim_ids[out.text_claim] if out.kind == "text" and out.text_claim
                              is not None else None))
    roots: list[SpecNode] = []
    for index, out in enumerate(nodes):
        current = built[index]
        if current is None:
            continue
        parent = built[out.parent] if out.parent is not None and 0 <= out.parent < index else None
        (parent.children if parent is not None else roots).append(current)
    return roots


def build_design_prototype(services: Services, llm: LlmNodes) -> Any:
    """Return the ``design_prototype`` dynamic node (input: ``{"problem_id"}``)."""
    catalog: DlsCatalog = services.dls_catalog or load_dls_catalog()

    def guard_requirement(node_input: Any) -> dict[str, Any]:
        data = node_input if isinstance(node_input, dict) else {}
        problem_id = data.get("problem_id") or find_problem_id(request_text(node_input))
        draft = committed_requirement(services, problem_id) if problem_id else None
        if draft is None:
            return {"error": "requirement_not_approved",
                    "message": "Prototype design starts from a requirement the PM approved and committed."}
        claims = [c for name in PROTOTYPE_FIELDS if name in draft.fields for c in draft.fields[name].claims]
        return {"problem_id": problem_id, "requirement_id": draft.requirement_id,
                "claims": [{"index": i, "claim_id": c.claim_id, "text": c.text} for i, c in enumerate(claims)]}

    def build_spec(node_input: dict[str, Any]) -> dict[str, Any]:
        raw = PrototypeOut.model_validate(node_input["raw"])
        claim_ids = [c["claim_id"] for c in node_input["guard"]["claims"]]
        patterns, blocked = [], []
        for pattern in raw.new_patterns:
            checked = services.verifier.check_claims(to_claim_drafts(pattern.justification))
            blocked += checked.blocked
            patterns.append(NewPattern(name=pattern.name, justification=checked.passed,
                                       nearest_candidates=[{"component_key": k} for k in pattern.nearest_candidates]))
        spec = PrototypeSpec(brief_id=new_id("brief"), requirement_id=node_input["guard"]["requirement_id"],
                             dls_library_file_key=catalog.file_key, dls_published_version=catalog.published_version,
                             screens=[PrototypeScreen(name=s.name, width=s.width, height=s.height,
                                                      nodes=_tree(s.nodes, claim_ids)) for s in raw.screens],
                             new_patterns=patterns)
        ref = services.subjects.put("prototype_spec", spec)
        services.ledger.record(LedgerEntryType.DRAFT_SAVED, actor=Actor(type="agent", id=services.agent_id),
                               subject_refs=[SubjectRef(type="prototype_spec", id=spec.spec_id,
                                                        sha256=subject_digest(spec))],
                               payload={"draft_ref": ref, "requirement_id": spec.requirement_id})
        return {"spec_ref": ref, "reuse_ratio": spec.reuse_ratio, "blocked": dump_blocked(blocked),
                "unknown_keys": sorted(set(spec.component_keys()) - catalog.keys)}

    def _target(subject: BaseModel) -> dict[str, Any]:
        assert isinstance(subject, PrototypeSpec)
        figma = services.settings.connectors.get("figma", {})
        return {"system": "figma", "team_id": figma.get("team_id"), "renderer": figma.get("renderer_path"),
                "page": f"PM Agent / {subject.requirement_id}"}

    def _surfaces(action: Any, subject: BaseModel, report: Any) -> list[dict[str, Any]]:
        assert isinstance(subject, PrototypeSpec)
        return ui.prototype_surface(subject, report, []) + ui.approval_surface(action, subject, report)

    tail = build_approval_nodes(services, prefix="prototype", action_type=FIGMA, subject_type="prototype_spec",
                                render=render_prototype_payload, target=_target, surfaces=_surfaces,
                                message="Approval required (PM + UX): render this DLS prototype in Figma.")

    @node(rerun_on_resume=True, name="design_prototype")
    async def design_prototype(ctx: Context, node_input: Any) -> dict[str, Any]:
        guard = await ctx.run_node(guard_requirement, node_input)
        if guard.get("error"):
            return {"status": guard["error"], "message": guard["message"]}
        pack = await ctx.run_node(_pack, {"problem_id": guard["problem_id"]})
        feedback: list[str] = []
        verified: dict[str, Any] = {}
        for _ in range(MAX_ATTEMPTS):
            raw = await ctx.run_node(llm["prototype_composer"], {
                "task": "prototype", "requirement_claims": [{"index": c["index"], "text": c["text"]}
                                                            for c in guard["claims"]],
                "dls_catalog": catalog.for_prompt(), "previous_attempt_problems": feedback, **pack})
            built = await ctx.run_node(build_spec, {"guard": guard, "raw": raw})
            verified = await ctx.run_node(tail.verify, {"subject_ref": built["spec_ref"]})
            if verified["overall"] == "PASS":
                break
            feedback = list(verified["failed"])
            if built["unknown_keys"]:
                feedback.append(f"component keys not in the DLS catalog: {built['unknown_keys']}")
        else:
            return {"status": "needs_input", "problems": feedback, "spec_ref": verified.get("subject_ref")}
        proposal = await ctx.run_node(tail.propose, verified)
        await ctx.run_node(tail.await_approval, proposal)
        outcome = await ctx.run_node(tail.commit, proposal)
        return {**outcome, "problem_id": guard["problem_id"], "spec_ref": verified["subject_ref"]}

    def _pack(node_input: dict[str, Any]) -> dict[str, Any]:
        return problem_pack(services, node_input["problem_id"])

    return design_prototype


def claims_by_id(draft: RequirementDraft) -> dict[str, str]:
    """Verified claim texts a compiled prototype may display."""
    return {c.claim_id: c.text for c in draft.all_claims() if isinstance(c, Claim)}
