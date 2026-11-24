"""Shared HITL tail for every state-changing capability (docs/07 §3–§4).

``verify_for_commit`` (V0–V10, commit-bound, exact payload) → ``propose`` (ledger; refused without a matching signed
PASS) → ``await_approval`` ⏸ (``RequestInput``; the decision itself goes browser → BFF → ledger, never through an
LLM) → ``commit_if_approved`` (authoritative ledger re-read, then the zero-LLM executor via A2A).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from google.adk.events import RequestInput
from google.adk.workflow import FunctionNode
from pydantic import BaseModel

from pm_agent.runtime import Services
from pm_agent.schemas import ActionStatus, Actor, LedgerEntryType, ProposedAction, VerificationReport

APPROVAL_SCHEMA: dict[str, Any] = {"type": "object", "properties": {
    "action_id": {"type": "string"}, "decision": {"type": "string"}}}

Render = Callable[[BaseModel], dict[str, Any]]
Target = Callable[[BaseModel], dict[str, Any]]
Surfaces = Callable[[ProposedAction, BaseModel, VerificationReport], list[dict[str, Any]]]
StatusHook = Callable[[BaseModel, str], None]


@dataclass(frozen=True)
class ApprovalNodes:
    verify: Any
    propose: Any
    await_approval: Any
    commit: Any


def build_approval_nodes(services: Services, *, prefix: str, action_type: str, subject_type: str, render: Render,
                         target: Target, surfaces: Surfaces, on_status: StatusHook | None = None,
                         message: str = "Approval required.") -> ApprovalNodes:
    """Nodes named ``verify_<prefix>``, ``propose_<prefix>``, ``await_<prefix>_approval``, ``commit_<prefix>``."""

    def _subject(ref: str) -> BaseModel:
        kind, subject = services.subjects.get(ref)
        if kind != subject_type:
            raise ValueError(f"{ref} is a {kind}, expected {subject_type}")
        return subject

    def verify(node_input: dict[str, Any]) -> dict[str, Any]:
        subject = _subject(node_input["subject_ref"])
        report = services.verifier.verify_subject(subject, subject_type=subject_type, commit_bound=True,
                                                  target_action=action_type, target_payload=render(subject))
        services.reports[report.report_id] = report
        services.ledger.record(LedgerEntryType.VERIFICATION_COMPLETED, actor=Actor(type="system", id="mcp-verifier"),
                               subject_refs=[report.subject_ref],
                               payload={"report_id": report.report_id, "overall": report.overall.value})
        return {"subject_ref": node_input["subject_ref"], "report_id": report.report_id,
                "overall": report.overall.value,
                "failed": [f"{c.check_id}: {c.details}" for c in report.checks if c.status.value == "FAIL"]}

    def propose(node_input: dict[str, Any]) -> dict[str, Any]:
        subject = _subject(node_input["subject_ref"])
        report = services.reports[node_input["report_id"]]
        action = services.approvals.propose(action_type=action_type, target=target(subject), payload=render(subject),
                                            draft_ref=node_input["subject_ref"], report=report,
                                            proposed_by=services.agent_id)
        if on_status:
            on_status(subject, "pending_approval")
        return {"action_id": action.action_id, "subject_ref": node_input["subject_ref"],
                "a2ui": surfaces(action, subject, report)}

    async def await_approval(node_input: dict[str, Any]) -> Any:
        yield RequestInput(message=message, payload=node_input, response_schema=APPROVAL_SCHEMA)

    def commit(node_input: dict[str, Any]) -> dict[str, Any]:
        """The resume payload is a hint; the ledger is the truth (H-2)."""
        action = services.approvals.get_action(node_input["action_id"])
        subject = _subject(node_input["subject_ref"])
        if action.status is not ActionStatus.APPROVED:
            if on_status:
                on_status(subject, action.status.value.lower())
            return {"status": action.status.value.lower(), "action_id": action.action_id}
        result = services.commit.execute(action.action_id)
        if on_status:
            on_status(subject, result.outcome)
        return {"status": result.outcome, "action_id": action.action_id, "external_ref": result.external_ref,
                "error": result.error}

    return ApprovalNodes(verify=FunctionNode(func=verify, name=f"verify_{prefix}"),
                         propose=FunctionNode(func=propose, name=f"propose_{prefix}"),
                         await_approval=FunctionNode(func=await_approval, name=f"await_{prefix}_approval"),
                         commit=FunctionNode(func=commit, name=f"commit_{prefix}"))
