"""Helpers shared by the capability workflows: evidence packs, claim (de)serialization, request parsing.

Workflow rules (docs/05 §6, verified by probe): orchestrator bodies are pure — every side effect happens inside a
child node run via ``ctx.run_node`` so it is checkpointed and never repeated on resume; node outputs are JSON
(models are dumped with ``mode="json"``); LLM nodes receive curated evidence packs built by code.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from typing import Any

from pydantic import BaseModel

from pm_agent.grounding.verifier import BlockedClaim
from pm_agent.ports import Services
from pm_agent.schemas import (
    Actor,
    Claim,
    LedgerEntryType,
    ProductProblem,
    RecordType,
    Signal,
    SignalStatus,
    SourceSystem,
    SubjectRef,
)
from pm_agent.schemas.subjects import subject_digest, subject_id

PROBLEM_ID = re.compile(r"\bprb_[0-9A-Z]{26}\b")
MAX_EVIDENCE_ITEMS = 24


def request_text(node_input: Any) -> str:
    """Plain text of a workflow input (ADK ``Content``, dict with ``request``/``text``, or str)."""
    if isinstance(node_input, str):
        return node_input
    if isinstance(node_input, dict):
        return str(node_input.get("request") or node_input.get("text") or "")
    parts = getattr(node_input, "parts", None) or []
    return "\n".join(getattr(part, "text", None) or "" for part in parts).strip()


def find_problem_id(text: str) -> str | None:
    match = PROBLEM_ID.search(text)
    return match.group(0) if match else None


def resolve_problem(services: Services, node_input: Any, *, ask: str) -> ProductProblem | dict[str, str]:
    """The problem named by ``{"problem_id"}`` or a ``prb_…`` id in the request — or an ``{error, message}`` output."""
    data = node_input if isinstance(node_input, dict) else {}
    problem_id = data.get("problem_id") or find_problem_id(request_text(node_input))
    if not problem_id:
        return {"error": "no_problem", "message": ask}
    try:
        return services.problems.get(problem_id)
    except LookupError:
        return {"error": "unknown_problem", "message": f"No product problem {problem_id}."}


def save_draft(services: Services, subject_type: str, subject: BaseModel, *, related: Iterable[SubjectRef] = (),
               payload: Mapping[str, Any] | None = None) -> str:
    """Store an immutable draft version and ledger ``DRAFT_SAVED`` (ids and hashes only); returns the subject ref."""
    ref = services.subjects.put(subject_type, subject)
    version = getattr(subject, "version", None)
    saved = SubjectRef(type=subject_type, id=subject_id(subject), version=version if isinstance(version, int) else None,
                       sha256=subject_digest(subject))
    services.ledger.record(LedgerEntryType.DRAFT_SAVED, actor=Actor(type="agent", id=services.agent_id),
                           subject_refs=[saved, *related], payload={"draft_ref": ref, **(payload or {})})
    return ref


def evidence_item(signal: Signal, evidence_id: str, text: str) -> dict[str, Any]:
    return {"evidence_id": evidence_id, "source_system": signal.source_system.value,
            "record_type": signal.record_type.value, "record_id": signal.source_record_id,
            "captured_utc": signal.captured_utc.isoformat(), "text": text}


_PRIMARY = {RecordType.INCIDENT: 0, RecordType.CASE: 0, RecordType.WORK_ITEM: 0, RecordType.REQUEST: 1,
            RecordType.EMAIL_MESSAGE: 2, RecordType.DOCUMENT: 2, RecordType.TRACKER_ROW: 3}


def _evidence_order(signal: Signal) -> tuple[int, int, str, str]:
    """Primary records before their comments/notes (titles and summaries come from the record itself)."""
    return (signal.parent_signal_id is not None, _PRIMARY.get(signal.record_type, 4),
            signal.captured_utc.isoformat(), signal.signal_id)


def signals_pack(services: Services, signal_ids: list[str], limit: int = MAX_EVIDENCE_ITEMS) -> dict[str, Any]:
    """Evidence (non-PM) and PM answers for a set of signals, chunk by chunk, active signals only."""
    evidence: list[dict[str, Any]] = []
    answers: list[dict[str, Any]] = []
    signals = [s for s in (services.evidence.get_signal(i) for i in signal_ids)
               if s is not None and s.status is SignalStatus.ACTIVE]
    for signal in sorted(signals, key=_evidence_order):
        signal_id = signal.signal_id
        target = answers if signal.source_system is SourceSystem.PM_INPUT else evidence
        for chunk in services.evidence.chunks_for(signal_id):
            target.append(evidence_item(signal, chunk.evidence_id, chunk.text))
    return {"evidence": evidence[:limit], "pm_answers": answers}


def problem_pack(services: Services, problem_id: str) -> dict[str, Any]:
    problem = services.problems.get(problem_id)
    return {"problem_id": problem_id, "problem_title": problem.title.text,
            **signals_pack(services, services.problems.signal_ids(problem_id))}


def dump_claims(claims: list[Claim]) -> list[dict[str, Any]]:
    return [c.model_dump(mode="json") for c in claims]


def load_claims(data: list[dict[str, Any]]) -> list[Claim]:
    return [Claim.model_validate(c) for c in data]


def dump_blocked(blocked: list[BlockedClaim]) -> list[dict[str, Any]]:
    """What the PM sees as 'removed — no supporting evidence' (ids, text and the failing check only)."""
    return [{"claim_id": b.claim_id, "text": b.text, "failed_checks": b.failed_checks, "reason": b.reason}
            for b in blocked]
