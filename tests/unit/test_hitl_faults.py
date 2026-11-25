"""Fault injection on the HITL write path (docs/07 §3, §7, §9): a failure never leaves half-applied state.

* **Ordering (S2)** — a signer or ledger failure in the middle of ``decide`` / ``consume_token`` leaves no ledger
  entry and no in-memory change, so the PM's (or executor's) retry succeeds instead of failing ``duplicate_approver``.
* **Commit records (S3)** — ``record_commit`` keeps its reserved keys authoritative, never fails on long vendor
  references after the vendor write succeeded, and only the executor that consumed the token may close the commit.
* **No silent exit (S1)** — once the token is consumed every exception ends in ``COMMIT_FAILED`` with a stable error
  made of codes and exception *type names* only; the in-process gateway types anticipated server failures.
"""
from __future__ import annotations

from typing import Any

import pytest
from pydantic import BaseModel

from pm_agent.connectors.base import ConnectorError
from pm_agent.gateway import InProcessToolGateway, ToolCallError
from pm_agent.grounding import JudgeUnavailable
from pm_agent.hitl.approvals import ApprovalError, _refusal_code, _TokenRecord  # pylint: disable=protected-access
from pm_agent.hitl.executor import CommitExecutor
from pm_agent.hitl.ledger import MAX_PAYLOAD_STRING, InMemoryLedgerStore
from pm_agent.mcp_servers.write_server import WriteToolServer
from pm_agent.placeholders import PlaceholderNotImplemented
from pm_agent.schemas import (
    ActionStatus,
    ActionType,
    ApprovalTokenClaims,
    Decision,
    LedgerEntryType,
    content_sha256,
    sha256_hex,
)
from tests.support import EXECUTOR_ID, PM, PM2, UX, make_hitl, prototype_spec

LEAK = "TVDHD-secret-content"  # appears in exception messages only — must never reach results or the ledger
ROGUE_ID = "spiffe://agents.example/rogue-executor"


@pytest.fixture(name="hitl")
def _hitl():  # type: ignore[no-untyped-def]
    return make_hitl()


def _approve(hitl, action, *people):  # type: ignore[no-untyped-def]
    outcome = None
    for person in people:
        outcome = hitl.approvals.decide(action_id=action.action_id, decision=Decision.APPROVE,
                                        assertion=hitl.identities.issue(person))
    return outcome


def _types(hitl, action_id: str) -> list[LedgerEntryType]:  # type: ignore[no-untyped-def]
    return [entry.entry_type for entry in hitl.ledger.query(action_id=action_id)]


def _fail_once(monkeypatch, when):  # type: ignore[no-untyped-def]
    """Make the ledger store refuse the first batch matching ``when`` (a transient AlloyDB/SQLite failure)."""
    real = InMemoryLedgerStore.append_many
    state = {"failed": False}

    def append_many(self, entries):  # type: ignore[no-untyped-def]
        if not state["failed"] and when([entry.entry_type for entry in entries]):
            state["failed"] = True
            raise RuntimeError("ledger store unavailable")
        return real(self, entries)

    monkeypatch.setattr(InMemoryLedgerStore, "append_many", append_many)
    return state


# ---------------------------------------------------------------------------------------- S2: ordering
def test_signer_failure_leaves_no_trace_and_the_pms_retry_succeeds(hitl, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    action = hitl.propose_ado()
    real_sign, calls = hitl.token_signer.sign, []

    def flaky_sign(data: bytes) -> bytes:
        calls.append(data)
        if len(calls) == 1:
            raise RuntimeError("KMS deadline exceeded")
        return real_sign(data)

    monkeypatch.setattr(hitl.token_signer, "sign", flaky_sign)
    with pytest.raises(ApprovalError) as failed:
        _approve(hitl, action, PM)
    assert failed.value.code == "signer_unavailable"
    current = hitl.approvals.get_action(action.action_id)
    assert (current.status, current.version) == (ActionStatus.PENDING_APPROVAL, action.version)
    assert hitl.approvals.decisions(action.action_id) == []
    assert _types(hitl, action.action_id) == [LedgerEntryType.ACTION_PROPOSED]
    retry = _approve(hitl, action, PM)
    assert retry.token_issued and (retry.approvals, retry.required) == (1, 1)
    assert _types(hitl, action.action_id) == [LedgerEntryType.ACTION_PROPOSED, LedgerEntryType.DECISION_RECORDED,
                                              LedgerEntryType.TOKEN_ISSUED]
    assert hitl.executor.execute(action.action_id, executor_id=EXECUTOR_ID).outcome == "committed"


def test_two_person_rule_survives_a_signer_failure_on_the_completing_approval(hitl, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    spec = prototype_spec()
    action = hitl.propose(spec, "prototype_spec", ActionType.FIGMA_RENDER_PROTOTYPE.value,
                          {"spec_sha256": content_sha256(spec), "reuse_ratio": 1.0})
    assert not _approve(hitl, action, PM).token_issued
    real_sign, calls = hitl.token_signer.sign, []

    def flaky_sign(data: bytes) -> bytes:
        calls.append(data)
        if len(calls) == 1:
            raise ConnectionError("KMS unreachable")
        return real_sign(data)

    monkeypatch.setattr(hitl.token_signer, "sign", flaky_sign)
    with pytest.raises(ApprovalError, match="signer_unavailable"):
        _approve(hitl, action, UX)
    assert len(hitl.approvals.decisions(action.action_id)) == 1  # only the PM's decision exists
    outcome = _approve(hitl, action, UX)
    assert outcome.token_issued and (outcome.approvals, outcome.required) == (2, 2)
    assert _types(hitl, action.action_id).count(LedgerEntryType.TOKEN_ISSUED) == 1


def test_decision_and_token_issue_are_written_all_or_nothing(hitl, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    action = hitl.propose_ado()
    state = _fail_once(monkeypatch, lambda types: LedgerEntryType.TOKEN_ISSUED in types)
    with pytest.raises(RuntimeError, match="ledger store unavailable"):
        _approve(hitl, action, PM)
    assert state["failed"] and _types(hitl, action.action_id) == [LedgerEntryType.ACTION_PROPOSED]
    assert hitl.approvals.get_action(action.action_id).status is ActionStatus.PENDING_APPROVAL
    assert hitl.approvals.decisions(action.action_id) == []
    assert _approve(hitl, action, PM).token_issued
    assert hitl.ledger.verify_chain().ok


def test_token_consumption_and_commit_start_are_written_all_or_nothing(hitl, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    action = hitl.propose_ado()
    _approve(hitl, action, PM)
    state = _fail_once(monkeypatch, lambda types: LedgerEntryType.TOKEN_CONSUMED in types)
    with pytest.raises(RuntimeError, match="ledger store unavailable"):
        hitl.executor.execute(action.action_id, executor_id=EXECUTOR_ID)
    assert state["failed"] and hitl.backend.calls == []
    assert hitl.approvals.get_action(action.action_id).status is ActionStatus.APPROVED  # token not burned
    assert LedgerEntryType.TOKEN_CONSUMED not in _types(hitl, action.action_id)
    assert LedgerEntryType.COMMIT_STARTED not in _types(hitl, action.action_id)
    assert hitl.executor.execute(action.action_id, executor_id=EXECUTOR_ID).outcome == "committed"
    assert _types(hitl, action.action_id).count(LedgerEntryType.TOKEN_CONSUMED) == 1


def test_rejection_and_revocation_are_written_together(hitl, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    action = hitl.propose_ado()
    _approve(hitl, action, PM)
    _fail_once(monkeypatch, lambda types: LedgerEntryType.TOKEN_REVOKED in types)
    reject = {"action_id": action.action_id, "decision": Decision.REJECT}
    with pytest.raises(RuntimeError):
        hitl.approvals.decide(**reject, assertion=hitl.identities.issue(PM2))
    assert hitl.approvals.get_action(action.action_id).status is ActionStatus.APPROVED
    outcome = hitl.approvals.decide(**reject, assertion=hitl.identities.issue(PM2))
    assert outcome.action.status is ActionStatus.REJECTED and not outcome.token_issued
    assert _types(hitl, action.action_id)[-2:] == [LedgerEntryType.DECISION_RECORDED, LedgerEntryType.TOKEN_REVOKED]
    assert hitl.executor.execute(action.action_id, executor_id=EXECUTOR_ID).error == "token_revoked"


def test_expired_token_is_ledgered_as_expired_before_the_refusal(hitl) -> None:  # type: ignore[no-untyped-def]
    action = hitl.propose_ado()
    _approve(hitl, action, PM)
    hitl.clock.advance(minutes=hitl.settings.hitl.token_ttl_minutes, seconds=1)
    with pytest.raises(ApprovalError) as expired:
        hitl.approvals.consume_token(action_id=action.action_id, consumer=EXECUTOR_ID)
    assert expired.value.code == "token_expired"
    assert _types(hitl, action.action_id)[-2:] == [LedgerEntryType.ACTION_EXPIRED, LedgerEntryType.POLICY_DENIED]
    assert hitl.approvals.get_action(action.action_id).status is ActionStatus.EXPIRED


def _claims(exp: int) -> ApprovalTokenClaims:
    return ApprovalTokenClaims(iss="i", sub="pm", aud="a", jti="tok_1", iat=exp - 900, exp=exp, act="act_1",
                               pah="sha256:p", tgt={}, scp=["s"], vrf="sha256:v")


@pytest.mark.parametrize(("status", "record", "code"), [
    (ActionStatus.APPROVED, None, "not_approved"),
    (ActionStatus.PENDING_APPROVAL, {}, "not_approved"),
    (ActionStatus.COMMITTING, {"consumed_by": EXECUTOR_ID}, "token_consumed"),
    (ActionStatus.REJECTED, {"revoked": True}, "token_revoked"),
    (ActionStatus.APPROVED, {"consumed_by": EXECUTOR_ID, "revoked": True}, "token_consumed"),  # consumed wins
    (ActionStatus.APPROVED, {"exp_offset": 0}, "token_expired"),  # exp is exclusive
    (ActionStatus.APPROVED, {"exp_offset": 1}, None),
])
def test_refusal_code_is_a_pure_ordered_decision(hitl, status, record, code) -> None:  # type: ignore[no-untyped-def]
    action = hitl.propose_ado().model_copy(update={"status": status})
    now = hitl.clock()
    token = None
    if record is not None:
        options = dict(record)
        exp = int(now.timestamp()) + options.pop("exp_offset", 600)
        token = _TokenRecord(token="t.o.k", claims=_claims(exp), **options)
    assert _refusal_code(action, token, now) == code
    assert _refusal_code(action, token, now) == code  # pure: same inputs, same answer, nothing mutated


# ----------------------------------------------------------------------------------- S3: record_commit
def _committing(hitl):  # type: ignore[no-untyped-def]
    action = hitl.propose_ado()
    _approve(hitl, action, PM)
    hitl.approvals.consume_token(action_id=action.action_id, consumer=EXECUTOR_ID)
    return action


def test_record_commit_keeps_reserved_keys_authoritative(hitl) -> None:  # type: ignore[no-untyped-def]
    action = _committing(hitl)
    hitl.approvals.record_commit(action_id=action.action_id, actor_id=EXECUTOR_ID, succeeded=True, report_id="rep_1",
                                 external_ref={"action_id": "act_victim", "reverification_report_id": "rep_forged",
                                               "external_id": "4711", "url": "https://dev.azure.com/o/p/4711",
                                               "error": "none", "note": "free text"})
    entry = hitl.ledger.query(entry_types=[LedgerEntryType.COMMIT_SUCCEEDED])[0]
    assert entry.payload == {"action_id": action.action_id, "reverification_report_id": "rep_1",
                             "external_id": "4711", "url": "https://dev.azure.com/o/p/4711"}
    assert hitl.ledger.query(action_id="act_victim") == []
    assert hitl.approvals.get_action(action.action_id).status is ActionStatus.COMMITTED


@pytest.mark.parametrize("value", ["9" * 600, "https://figma.example/" + "\u00e9" * 600, "\u0000" * 300,
                                   "\U0001f600" * 200])
def test_record_commit_shortens_long_refs_instead_of_failing_after_the_write(hitl, value: str) -> None:  # type: ignore[no-untyped-def]
    action = _committing(hitl)
    updated = hitl.approvals.record_commit(action_id=action.action_id, actor_id=EXECUTOR_ID, succeeded=True,
                                           external_ref={"external_id": value, "url": value}, report_id="r" * 900)
    assert updated.status is ActionStatus.COMMITTED
    payload = hitl.ledger.query(entry_types=[LedgerEntryType.COMMIT_SUCCEEDED])[0].payload
    for key in ("external_id", "url", "reverification_report_id"):
        assert len(payload[key]) <= MAX_PAYLOAD_STRING
    assert payload["external_id"].endswith("sha256:" + sha256_hex(value))  # verifiable against the vendor record
    assert payload["external_id"][:8] == value[:8]  # a readable prefix survives
    assert hitl.ledger.verify_chain().ok


def test_record_commit_keeps_short_refs_verbatim(hitl) -> None:  # type: ignore[no-untyped-def]
    action = _committing(hitl)
    ref = {"external_id": "x" * MAX_PAYLOAD_STRING, "url": "https://dev.azure.com/o/p/_workitems/edit/1"}
    hitl.approvals.record_commit(action_id=action.action_id, actor_id=EXECUTOR_ID, succeeded=True, external_ref=ref)
    payload = hitl.ledger.query(entry_types=[LedgerEntryType.COMMIT_SUCCEEDED])[0].payload
    assert (payload["external_id"], payload["url"]) == (ref["external_id"], ref["url"])


def test_only_the_consuming_executor_may_close_the_commit(hitl) -> None:  # type: ignore[no-untyped-def]
    action = _committing(hitl)
    with pytest.raises(ApprovalError) as rogue:
        hitl.approvals.record_commit(action_id=action.action_id, actor_id=ROGUE_ID, succeeded=True,
                                     external_ref={"external_id": "1"})
    assert rogue.value.code == "not_committer"
    assert hitl.approvals.get_action(action.action_id).status is ActionStatus.COMMITTING
    denied = hitl.ledger.query(entry_types=[LedgerEntryType.POLICY_DENIED], action_id=action.action_id)
    assert [(e.payload["code"], e.actor.id) for e in denied] == [("not_committer", ROGUE_ID)]
    updated = hitl.approvals.record_commit(action_id=action.action_id, actor_id=EXECUTOR_ID, succeeded=False,
                                           error="x" * 1000, changes_requested=True)
    assert updated.status is ActionStatus.CHANGES_REQUESTED
    failed = hitl.ledger.query(entry_types=[LedgerEntryType.COMMIT_FAILED])[0]
    assert len(failed.payload["error"]) <= 256 and failed.actor.id == EXECUTOR_ID


# ---------------------------------------------------------------------------------- S1: no silent exit
def _raising_handler(exc: BaseException, *, on_preflight: bool = False):  # type: ignore[no-untyped-def]
    def handler(*, payload: Any, target: dict[str, Any], validate_only: bool,
                idempotency_key: str) -> dict[str, Any]:
        del payload, target, idempotency_key
        if validate_only and not on_preflight:
            return {"status": "valid"}
        raise exc

    return handler


def _executor(hitl, *, handler=None, read_back=None, verifier=None) -> CommitExecutor:  # type: ignore[no-untyped-def]
    server = WriteToolServer("mcp-ado-write", hitl.guard, {
        "create_work_item": handler or hitl.backend.handler("create_work_item"),
        "add_relations": hitl.backend.handler("add_relations")})
    return CommitExecutor(approvals=hitl.approvals, verifier=verifier or hitl.verifier, subjects=hitl.subjects,
                          catalog=hitl.catalog, gateway=InProcessToolGateway({"mcp-ado-write": server}),
                          token_keys={hitl.token_signer.key_id: hitl.token_signer.public_key_pem()},
                          settings=hitl.settings, read_back=read_back, clock=hitl.clock)


class _BrokenVerifier:
    def verify_subject(self, *args: Any, **kwargs: Any) -> Any:
        del args, kwargs
        raise JudgeUnavailable(f"judge quota exhausted while reading {LEAK}")


def _raise(exc: BaseException):  # type: ignore[no-untyped-def]
    def read_back(*args: Any) -> bool:
        del args
        raise exc

    return read_back


@pytest.mark.parametrize(("wiring", "error"), [
    (lambda: {"handler": _raising_handler(ConnectorError("unavailable", "HTTP 503"))}, "write unavailable"),
    (lambda: {"handler": _raising_handler(ConnectorError("rate_limited", "HTTP 429", retry_after_seconds=3))},
     "write rate_limited"),
    (lambda: {"handler": _raising_handler(PlaceholderNotImplemented("Q-04", "ado", LEAK))}, "write unavailable"),
    (lambda: {"handler": _raising_handler(ValueError(LEAK))}, "write validation_failed"),
    (lambda: {"handler": _raising_handler(RuntimeError(LEAK))}, "write outcome unknown: RuntimeError"),
    (lambda: {"handler": _raising_handler(AttributeError(LEAK))}, "write outcome unknown: AttributeError"),
    (lambda: {"read_back": _raise(ConnectionError(LEAK))}, "write outcome unknown: ConnectionError"),
    (lambda: {"handler": _raising_handler(ConnectorError("unavailable"), on_preflight=True)}, "preflight unavailable"),
    (lambda: {"handler": _raising_handler(RuntimeError(LEAK), on_preflight=True)}, "preflight error: RuntimeError"),
    (lambda: {"verifier": _BrokenVerifier()}, "preflight error: JudgeUnavailable"),
])
def test_every_exit_after_consumption_records_commit_failed(hitl, wiring, error: str) -> None:  # type: ignore[no-untyped-def]
    action = hitl.propose_ado()
    _approve(hitl, action, PM)
    result = _executor(hitl, **wiring()).execute(action.action_id, executor_id=EXECUTOR_ID)
    assert (result.outcome, result.error) == ("failed", error)
    assert hitl.approvals.get_action(action.action_id).status is ActionStatus.FAILED
    failed = hitl.ledger.query(entry_types=[LedgerEntryType.COMMIT_FAILED], action_id=action.action_id)
    assert [e.payload["error"] for e in failed] == [error]
    assert LEAK not in hitl.ledger_dump() and LEAK not in str(result)
    assert hitl.ledger.commits_without_consumed_token() == []


def test_a_failed_commit_cannot_be_retried_with_the_same_token(hitl) -> None:  # type: ignore[no-untyped-def]
    action = hitl.propose_ado()
    _approve(hitl, action, PM)
    assert _executor(hitl, handler=_raising_handler(RuntimeError("boom"))).execute(
        action.action_id, executor_id=EXECUTOR_ID).outcome == "failed"
    again = hitl.executor.execute(action.action_id, executor_id=EXECUTOR_ID)
    assert (again.outcome, again.error) == ("refused", "token_consumed") and hitl.backend.creates() == []


# ---------------------------------------------------------------------------------- gateway translation
class _Server:
    name = "mcp-fake-write"

    def __init__(self, exc: BaseException) -> None:
        self.exc = exc

    def call(self, tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
        del tool, arguments
        raise self.exc


class _Strict(BaseModel):
    count: int


def _validation_error() -> Exception:
    try:
        _Strict.model_validate({"count": LEAK})
    except ValueError as exc:
        return exc
    raise AssertionError("expected a ValidationError")  # pragma: no cover


@pytest.mark.parametrize(("exc", "code"), [
    (ConnectorError("rate_limited", "HTTP 429", retry_after_seconds=2.0), "rate_limited"),
    (ConnectorError("auth_failed", "HTTP 401"), "auth_failed"),
    (PlaceholderNotImplemented("Q-04", "ado write transport", LEAK), "unavailable"),
    (KeyError(LEAK), "not_found"),
    (LookupError(LEAK), "not_found"),
    (ValueError(LEAK), "validation_failed"),
    (TypeError(LEAK), "validation_failed"),
    (_validation_error(), "validation_failed"),
    (ApprovalError("token_consumed", LEAK), "token_consumed"),
])
def test_in_process_gateway_types_anticipated_server_failures(exc: Exception, code: str) -> None:
    gateway = InProcessToolGateway({"mcp-fake-write": _Server(exc)})
    with pytest.raises(ToolCallError) as typed:
        gateway.call("mcp-fake-write", "tool", {})
    assert typed.value.code == code and typed.value.__cause__ is exc
    assert LEAK not in str(typed.value)


def test_in_process_gateway_passes_typed_errors_through_and_lets_unknown_ones_escape() -> None:
    typed = ToolCallError("unauthorized", "replayed")
    with pytest.raises(ToolCallError) as same:
        InProcessToolGateway({"s": _Server(typed)}).call("s", "tool", {})
    assert same.value is typed
    with pytest.raises(RuntimeError):
        InProcessToolGateway({"s": _Server(RuntimeError("unanticipated"))}).call("s", "tool", {})
    with pytest.raises(ToolCallError) as missing:
        InProcessToolGateway({}).call("nowhere", "tool", {})
    assert missing.value.code == "not_found"
