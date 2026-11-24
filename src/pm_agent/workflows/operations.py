"""Intelligent Product Operations (docs/05 §4.5–4.6; FR-C1…C3, C5–C8, AC5, AC7).

Deterministic reference versions (no model calls), every statement a verified claim:
* ``investigate_issue`` — KB-first lookup, correlated signals (hard links + similarity), cited timeline → pack.
* ``trace_roadmap`` — evidence chain signal → problem → ADO work item → KB article from store + ledger truth.
* ``surface_trends`` — allow-listed views over the problem store using V6-checked aggregate claims.
The LLM refinements in docs/05 (engagement advisor, outcome classifier, trace linker, trend narrator) plug in as
extra nodes without changing these contracts.
"""

from __future__ import annotations

from typing import Any

from pm_agent.a2ui import surfaces as ui
from pm_agent.correlation.aggregates import first_sentence
from pm_agent.runtime import Services
from pm_agent.schemas import (
    ActionType,
    CitedQuote,
    ClaimDraft,
    ClaimKind,
    InvestigationPack,
    LedgerEntryType,
    LinkType,
    Signal,
    SignalLink,
    TraceLink,
    TraceLinkType,
)
from pm_agent.workflows.common import dump_blocked, dump_claims, find_problem_id, request_text

MAX_RELATED = 10
COMMIT_LINKS = {ActionType.ADO_CREATE_WORK_ITEM: ("ado_work_item", TraceLinkType.SPECIFIED_BY),
                ActionType.KB_PUBLISH_ARTICLE: ("kb_article", TraceLinkType.CAPTURED_AS_KNOWLEDGE)}


def _timeline_claim(services: Services, signal: Signal) -> ClaimDraft | None:
    chunks = services.evidence.chunks_for(signal.signal_id)
    if not chunks:
        return None
    sentence = first_sentence(chunks[0].text)
    return ClaimDraft(text=sentence, kind=ClaimKind.FACT,
                      citations=[CitedQuote(evidence_id=chunks[0].evidence_id, quote=sentence)])


def build_investigate_issue(services: Services) -> Any:
    """W5 node: KB-first, correlated signals and a cited timeline assembled into an ``InvestigationPack``."""
    def investigate_issue(node_input: Any) -> dict[str, Any]:
        data = node_input if isinstance(node_input, dict) else {}
        text = request_text(node_input)
        trigger = services.evidence.get_signal(str(data.get("signal_id") or ""))
        if trigger is None:
            hits = services.evidence.search(text, top_k=1)
            trigger = services.evidence.get_signal(hits[0].signal_id) if hits else None
        if trigger is None:
            return {"status": "no_evidence", "message": "No ingested signal matches this issue yet."}
        prior = [c.model_dump(mode="json") for c in services.near_duplicates.find(trigger.raw_text)
                 if c.system == "kb"]  # KB first (FR-C5)
        hard = services.correlation.hard_links(trigger, {})
        problem_ids = {c.problem_id for c in hard}
        own_problem = services.problems.problem_for_signal(trigger.signal_id)
        if own_problem:
            problem_ids.add(own_problem)
        related_ids = [s for pid in sorted(problem_ids) for s in services.problems.signal_ids(pid)
                       if s != trigger.signal_id][:MAX_RELATED]
        related = [services.evidence.get_signal(s) for s in related_ids]
        signals = sorted([trigger, *[s for s in related if s]], key=lambda s: (s.captured_utc, s.signal_id))
        drafts = [d for d in (_timeline_claim(services, s) for s in signals) if d]
        checked = services.verifier.check_claims(drafts)
        pack = InvestigationPack(trigger_signal_id=trigger.signal_id,
                                 related=[SignalLink(signal_id=s.signal_id, link_type=LinkType.HARD_REF, score=1.0,
                                                     linked_by="investigate_issue") for s in signals
                                          if s.signal_id != trigger.signal_id],
                                 timeline=checked.passed)
        return {"status": "ok", "pack": pack.model_dump(mode="json"), "prior_resolutions": prior,
                "blocked": dump_blocked(checked.blocked), "a2ui": ui.investigation_surface(pack)}

    return investigate_issue


def build_trace_roadmap(services: Services) -> Any:
    """T node: the evidence chain for one problem from the store and the ledger (no inference)."""
    def trace_roadmap(node_input: Any) -> dict[str, Any]:
        data = node_input if isinstance(node_input, dict) else {}
        problem_id = data.get("problem_id") or find_problem_id(request_text(node_input))
        if not problem_id:
            return {"status": "no_problem", "message": "Which product problem should be traced?"}
        links = [TraceLink(from_type="signal", from_id=signal_id, to_type="product_problem", to_id=problem_id,
                           link_type=TraceLinkType.SIGNAL_OF, status="confirmed")
                 for signal_id in services.problems.signal_ids(problem_id)]
        for entry in services.ledger.query(entry_types=[LedgerEntryType.COMMIT_SUCCEEDED]):
            action = services.approvals.get_action(str(entry.payload.get("action_id")))
            if action.action_type not in COMMIT_LINKS:
                continue
            _, subject = services.subjects.get(action.draft_ref)
            if getattr(subject, "problem_id", None) != problem_id:
                continue
            to_type, link_type = COMMIT_LINKS[action.action_type]
            links.append(TraceLink(from_type="product_problem", from_id=problem_id, to_type=to_type,
                                   to_id=str(entry.payload.get("external_id", action.action_id)),
                                   link_type=link_type, status="confirmed"))
        return {"status": "ok", "problem_id": problem_id, "links": [link.model_dump(mode="json") for link in links],
                "roadmap_decisions": "recorded by the PM in the workbench (ROADMAP_TRACE_LINK); never by the agent"}

    return trace_roadmap


def build_surface_trends(services: Services) -> Any:
    """R node: advisory view of the most-reported problems built from V6-checked aggregates."""
    def surface_trends(node_input: Any) -> dict[str, Any]:
        """Top problems by linked signals; only problems that re-verify (V0–V10) contribute claims."""
        del node_input
        ranked = sorted(services.problems.list_problems(), key=lambda p: (-len(p.linked_signals), p.problem_id))[:5]
        verified = [p for p in ranked if services.verifier.verify_subject(
            p, subject_type="product_problem").overall.value == "PASS"]
        claims = [c for p in verified for c in (p.request_frequency, p.affected_customers) if c is not None]
        return {"status": "ok" if verified else "no_problems", "advisory": True,
                "top_problems": [{"problem_id": p.problem_id, "title": p.title.text,
                                  "signals": len(p.linked_signals)} for p in verified],
                "claims": dump_claims(claims), "withheld": len(ranked) - len(verified)}

    return surface_trends
