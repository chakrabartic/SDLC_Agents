"""``commit-executor`` core — the zero-LLM write path (docs/07 §7).

Steps (each one is a function node in ``pm_agent.executor_app``):
``load_action → consume_token → verify_token → reverify → preflight → execute → read_back → record``.

Once the token is consumed every exit records ``COMMIT_SUCCEEDED`` or ``COMMIT_FAILED`` — there is no silent path.
Transient vendor errors are retried *inside* a single execution by the connector (``Retry-After``); a FAILED action
needs a fresh approval, so one token can never back two writes.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from pm_agent.gateway import ToolCallError, ToolGateway
from pm_agent.grounding.verifier import Verifier
from pm_agent.hitl.approvals import ApprovalError, ApprovalService, IssuedToken
from pm_agent.hitl.catalog import ActionCatalog, ActionSpec
from pm_agent.hitl.subjects import SubjectStore
from pm_agent.hitl.tokens import TokenError, verify_token
from pm_agent.schemas import (
    ApprovalTokenClaims,
    CommitOutcome,
    CommitResult,
    ProposedAction,
    VerificationOutcome,
    utcnow,
)
from pm_agent.settings import Settings

__all__ = ["CommitAborted", "CommitContext", "CommitExecutor", "CommitResult", "ReadBack"]

class CommitAborted(Exception):
    """Raised by a step to stop the commit; ``changes_requested`` sends the draft back to the PM."""

    def __init__(self, error: str, *, changes_requested: bool = False) -> None:
        super().__init__(error)
        self.error = error
        self.changes_requested = changes_requested


@dataclass
class CommitContext:
    action: ProposedAction
    spec: ActionSpec
    executor_id: str
    issued: IssuedToken | None = None
    claims: ApprovalTokenClaims | None = None
    report_id: str | None = None
    result: dict[str, Any] | None = None


ReadBack = Callable[[ActionSpec, ProposedAction, dict[str, Any]], bool]
"""Re-read the created object through a *read* server and compare key fields with the approved payload."""


class CommitExecutor:  # pylint: disable=too-many-instance-attributes
    def __init__(self, *, approvals: ApprovalService, verifier: Verifier, subjects: SubjectStore,
                 catalog: ActionCatalog, gateway: ToolGateway, token_keys: Mapping[str, bytes], settings: Settings,
                 read_back: ReadBack | None = None, clock: Callable[[], datetime] = utcnow) -> None:
        self._approvals = approvals
        self._verifier = verifier
        self._subjects = subjects
        self._catalog = catalog
        self._gateway = gateway
        self._keys = dict(token_keys)
        self._settings = settings
        self._read_back = read_back
        self._clock = clock

    def execute(self, action_id: str, *, executor_id: str) -> CommitResult:
        """Run every step in order; refusals before consumption leave no commit trace beyond ``POLICY_DENIED``."""
        try:
            ctx = self.load_action(action_id, executor_id)
            self.consume_token(ctx)
        except ApprovalError as exc:
            return CommitResult(action_id=action_id, outcome="refused", error=exc.code)
        wrote = False
        try:
            self.verify_token(ctx)
            self.reverify(ctx)
            self.preflight(ctx)
            wrote = True
            self.execute_write(ctx)
            self.read_back(ctx)
        except CommitAborted as abort:
            return self.record_failure(ctx, abort)
        except Exception as exc:  # pylint: disable=broad-exception-caught  # no silent exit after token consumption
            # Never echo exception messages into the ledger (they may carry content); type names only (docs/07 §9).
            prefix = "write outcome unknown" if wrote else "preflight error"
            return self.record_failure(ctx, CommitAborted(f"{prefix}: {type(exc).__name__}"))
        return self.record_success(ctx)

    # ------------------------------------------------------------------------------------------------ steps
    def load_action(self, action_id: str, executor_id: str) -> CommitContext:
        action = self._approvals.get_action(action_id)
        return CommitContext(action=action, spec=self._catalog.get(action.action_type.value), executor_id=executor_id)

    def consume_token(self, ctx: CommitContext) -> None:
        ctx.issued = self._approvals.consume_token(action_id=ctx.action.action_id, consumer=ctx.executor_id)

    def verify_token(self, ctx: CommitContext) -> None:
        """First of two independent token checks (the write server repeats it)."""
        assert ctx.issued is not None
        try:
            ctx.claims = verify_token(ctx.issued.token, keys=self._keys, audience=self._settings.hitl.token_audience,
                                      issuer=self._settings.hitl.token_issuer, scope=ctx.spec.scope,
                                      payload=ctx.action.payload, action_id=ctx.action.action_id,
                                      now=self._clock().timestamp(), leeway=self._settings.hitl.clock_skew_seconds)
        except TokenError as exc:
            raise CommitAborted(f"token_{exc.code}") from exc

    def reverify(self, ctx: CommitContext) -> None:
        """Re-run the Verifier on the stored subject: unchanged since approval and still PASS (evidence alive)."""
        assert ctx.claims is not None
        try:
            subject_type, subject = self._subjects.get(ctx.action.draft_ref)
        except LookupError as exc:
            raise CommitAborted("subject missing", changes_requested=True) from exc
        report = self._verifier.verify_subject(subject, subject_type=subject_type, commit_bound=True,
                                               target_action=ctx.action.action_type.value,
                                               target_payload=ctx.action.payload)
        ctx.report_id = report.report_id
        if report.subject_sha256 != ctx.claims.vrf:
            raise CommitAborted("subject changed since approval", changes_requested=True)
        if report.target_payload_sha256 != ctx.claims.pah:
            raise CommitAborted("payload hash differs from the approved payload")
        if report.overall is not VerificationOutcome.PASS:
            failed = [c.check_id for c in report.checks if c.status.value not in {"PASS", "SKIPPED"}]
            raise CommitAborted(f"re-verification {report.overall.value}: {failed}", changes_requested=True)

    def _call(self, ctx: CommitContext, *, validate_only: bool) -> dict[str, Any]:
        assert ctx.issued is not None
        arguments = {"payload": ctx.action.payload, "action_id": ctx.action.action_id,
                     "approval_token": ctx.issued.token, "validate_only": validate_only}
        try:
            return self._gateway.call(ctx.spec.server, ctx.spec.tool, arguments)
        except ToolCallError as exc:
            raise CommitAborted(f"{'preflight' if validate_only else 'write'} {exc.code}") from exc

    def preflight(self, ctx: CommitContext) -> None:
        if ctx.spec.preflight:
            self._call(ctx, validate_only=True)

    def execute_write(self, ctx: CommitContext) -> None:
        ctx.result = self._call(ctx, validate_only=False)

    def read_back(self, ctx: CommitContext) -> None:
        if self._read_back is not None and ctx.result is not None \
                and not self._read_back(ctx.spec, ctx.action, ctx.result):
            raise CommitAborted("read-back mismatch after write")

    def record_success(self, ctx: CommitContext) -> CommitResult:
        result = ctx.result or {}
        external: dict[str, str] = {key: str(result[key]) for key in ("external_id", "url") if key in result}
        self._approvals.record_commit(action_id=ctx.action.action_id, actor_id=ctx.executor_id, succeeded=True,
                                      external_ref=external, report_id=ctx.report_id)
        return CommitResult(action_id=ctx.action.action_id, outcome="committed", external_ref=external)

    def record_failure(self, ctx: CommitContext, abort: CommitAborted) -> CommitResult:
        self._approvals.record_commit(action_id=ctx.action.action_id, actor_id=ctx.executor_id, succeeded=False,
                                      error=abort.error, changes_requested=abort.changes_requested,
                                      report_id=ctx.report_id)
        outcome: CommitOutcome = "changes_requested" if abort.changes_requested else "failed"
        return CommitResult(action_id=ctx.action.action_id, outcome=outcome, error=abort.error)
