"""Concurrency tests for single-use HITL token enforcement (docs/07 §6, H-5).

A ``threading.Barrier`` releases all worker threads at once to maximise lock contention against:
* ``ApprovalService.consume_token`` — exactly one executor wins the token; every other call gets ``token_consumed``;
* ``WriteToolGuard.authorize(validate_only=False)`` — exactly one real execution passes; every replay gets ``replayed``.
"""
from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor

from pm_agent.hitl.approvals import ApprovalError
from pm_agent.hitl.tokens import TokenError
from pm_agent.schemas import Decision, LedgerEntryType
from tests.support import EXECUTOR_ID, PM, make_hitl

WORKERS = 16


def test_concurrent_consume_token_allows_exactly_one_executor() -> None:
    hitl = make_hitl()
    action = hitl.propose_ado()
    hitl.approvals.decide(action_id=action.action_id, decision=Decision.APPROVE, assertion=hitl.identities.issue(PM))
    barrier = threading.Barrier(WORKERS)

    def attempt(worker: int) -> tuple[str, str]:
        barrier.wait()
        try:
            issued = hitl.approvals.consume_token(action_id=action.action_id, consumer=f"{EXECUTOR_ID}-{worker}")
            return ("ok", issued.claims.jti)
        except ApprovalError as exc:
            return ("err", exc.code)

    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        outcomes = list(pool.map(attempt, range(WORKERS)))

    wins = [val for kind, val in outcomes if kind == "ok"]
    losses = [val for kind, val in outcomes if kind == "err"]
    assert len(wins) == 1 and len(losses) == WORKERS - 1
    assert all(code == "token_consumed" for code in losses)
    assert len(hitl.ledger.query(entry_types=[LedgerEntryType.TOKEN_CONSUMED], action_id=action.action_id)) == 1
    assert hitl.ledger.verify_chain().ok


def test_concurrent_write_guard_allows_exactly_one_real_execution() -> None:
    hitl = make_hitl()
    action = hitl.propose_ado()
    hitl.approvals.decide(action_id=action.action_id, decision=Decision.APPROVE, assertion=hitl.identities.issue(PM))
    issued = hitl.approvals.consume_token(action_id=action.action_id, consumer=EXECUTOR_ID)
    barrier = threading.Barrier(WORKERS)

    def attempt(_: int) -> tuple[str, str]:
        barrier.wait()
        try:
            claims = hitl.guard.authorize(scope="ado_write.create_work_item", payload=action.payload,
                                          token=issued.token, action_id=action.action_id, validate_only=False)
            return ("ok", claims.jti)
        except TokenError as exc:
            return ("err", exc.code)

    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        outcomes = list(pool.map(attempt, range(WORKERS)))

    wins = [val for kind, val in outcomes if kind == "ok"]
    losses = [val for kind, val in outcomes if kind == "err"]
    assert len(wins) == 1 and len(losses) == WORKERS - 1
    assert all(code == "replayed" for code in losses)
