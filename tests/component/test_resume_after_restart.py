"""Crash-then-resume for the HITL tail (docs/05 §6: node outputs are checkpointed; resumes may land anywhere).

A transient failure between ``verify`` and ``propose`` must be recoverable by resuming the same invocation on a
freshly built app: the signed report is carried by the checkpointed ``verify`` output — never by process memory —
and the verification itself is not repeated.
"""
from __future__ import annotations

from typing import Any

import pytest

from pm_agent.agents.offline import ScriptedModelProvider
from pm_agent.core_app import build_core_app
from pm_agent.hitl.resume import resume_content
from pm_agent.schemas import LedgerEntryType, ProposedAction
from tests.component.helpers import AppSession
from tests.component.test_define_requirement import ANSWER, SCRIPT, _services_with_problem
from tests.support import PM

pytestmark = pytest.mark.component


class FlakyApprovals:
    """The agent's ``ApprovalsPort`` whose first ``propose`` fails like a ledger outage or a process restart."""

    def __init__(self, inner: Any) -> None:
        self._inner = inner
        self.failures_left = 1

    def propose(self, **kwargs: Any) -> ProposedAction:
        if self.failures_left:
            self.failures_left -= 1
            raise ConnectionError("mcp-ledger unavailable")
        return self._inner.propose(**kwargs)  # type: ignore[no-any-return]

    def get_action(self, action_id: str) -> ProposedAction:
        return self._inner.get_action(action_id)  # type: ignore[no-any-return]


async def test_resume_after_a_failed_propose_uses_the_checkpointed_signed_report() -> None:
    services, problem = _services_with_problem()
    services.approvals = FlakyApprovals(services.approval_service)
    session = AppSession(build_core_app(services, ScriptedModelProvider(SCRIPT)))
    first = await session.send({"capability": "define_requirement", "problem_id": problem.problem_id})
    [ask] = first.interrupts
    answer = {"answers": [{"question_id": ask.payload["questions"][0]["question_id"],
                           "checklist_item": "acceptance_criteria", "text": ANSWER}],
              "assertion": services.identity.issue(PM)}
    with pytest.raises(ConnectionError):
        await session.send(resume_content(ask.interrupt_id, answer), invocation_id=first.invocation_id)
    verified = services.ledger.query(entry_types=[LedgerEntryType.VERIFICATION_COMPLETED],
                                     subject_type="requirement_draft")
    assert len(verified) == 1 and services.approval_service.pending() == []

    # "Restart": a brand-new app (no closure state survives) resumes the same invocation from the session's events.
    session.runner.app = build_core_app(services, ScriptedModelProvider(SCRIPT))  # type: ignore[misc]
    resumed = await session.send(resume_content(ask.interrupt_id, answer), invocation_id=first.invocation_id)
    [approval] = resumed.interrupts
    action = services.approval_service.get_action(approval.payload["action_id"])
    assert action.verification_report_id == verified[0].payload["report_id"]
    assert len(services.ledger.query(entry_types=[LedgerEntryType.VERIFICATION_COMPLETED],
                                     subject_type="requirement_draft")) == 1, "verification must not be repeated"
