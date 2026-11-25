"""Ledger hash chain, approval tokens (JWS ES256) and the action catalog (docs/07 §2, §6, §8)."""
from __future__ import annotations

import json
import sqlite3
import time

import pytest

from pm_agent.crypto import LocalEcdsaSigner, b64url_encode
from pm_agent.hitl.catalog import ActionCatalog, UnknownAction
from pm_agent.hitl.ledger import GENESIS_HASH, InMemoryLedgerStore, Ledger, NewEntry, SqliteLedgerStore
from pm_agent.hitl.tokens import TokenError, mint_token, verify_token
from pm_agent.schemas import ActionType, Actor, ApprovalTokenClaims, LedgerEntryType, SubjectRef, content_sha256
from tests.support import local_settings

AGENT = Actor(type="agent", id="spiffe://agents.example/pm-agent-core")
EXECUTOR = Actor(type="agent", id="spiffe://agents.example/commit-executor")
PROBLEM = SubjectRef(type="product_problem", id="prb_1")
PAYLOAD = {"work_item_type": "Requirement", "fields": {"System.Title": "TVDHD not populated"}}
SCOPE = "ado_write.create_work_item"


# --------------------------------------------------------------------------------------------------- ledger
def test_ledger_entries_form_a_verifiable_hash_chain() -> None:
    ledger = Ledger(InMemoryLedgerStore())
    first = ledger.record(LedgerEntryType.DRAFT_SAVED, actor=AGENT, subject_refs=[PROBLEM], payload={"v": 1})
    second = ledger.record(LedgerEntryType.ACTION_PROPOSED, actor=AGENT, payload={"action_id": "act_1"})
    assert (first.seq, second.seq) == (1, 2)
    assert first.prev_hash == GENESIS_HASH and second.prev_hash == first.entry_hash
    assert ledger.verify_chain().ok


def test_ledger_detects_tampering_in_storage() -> None:
    store = InMemoryLedgerStore()
    ledger = Ledger(store)
    for index in range(3):
        ledger.record(LedgerEntryType.ACTION_PROPOSED, actor=AGENT, payload={"action_id": f"act_{index}"})
    store.entries_by_seq[2] = store.entries_by_seq[2].model_copy(update={"payload": {"action_id": "act_forged"}})
    result = ledger.verify_chain()
    assert not result.ok and result.first_bad_seq == 2


def test_sqlite_ledger_is_append_only_queryable_and_durable(tmp_path) -> None:  # type: ignore[no-untyped-def]
    path = tmp_path / "ledger.db"
    store = SqliteLedgerStore(path)
    ledger = Ledger(store)
    ledger.record(LedgerEntryType.DRAFT_SAVED, actor=AGENT, subject_refs=[PROBLEM], payload={"v": 1})
    ledger.record(LedgerEntryType.ACTION_PROPOSED, actor=AGENT, subject_refs=[PROBLEM], payload={"action_id": "act_1"})
    ledger.record(LedgerEntryType.ACTION_PROPOSED, actor=AGENT, payload={"action_id": "act_2"})
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        store.connection.execute("UPDATE ledger_entries SET payload = '{}' WHERE seq = 1")
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        store.connection.execute("DELETE FROM ledger_entries")
    by_problem = ledger.query(subject_type="product_problem", subject_id="prb_1")
    assert [e.entry_type for e in by_problem] == [LedgerEntryType.DRAFT_SAVED, LedgerEntryType.ACTION_PROPOSED]
    assert [e.seq for e in ledger.query(action_id="act_2")] == [3]
    reopened = Ledger(SqliteLedgerStore(path))
    assert reopened.verify_chain().ok and reopened.head() is not None and reopened.head().seq == 3


def test_control_query_flags_commits_without_consumed_tokens() -> None:
    ledger = Ledger(InMemoryLedgerStore())
    ledger.record(LedgerEntryType.TOKEN_CONSUMED, actor=EXECUTOR, payload={"action_id": "act_ok"})
    ledger.record(LedgerEntryType.COMMIT_SUCCEEDED, actor=EXECUTOR, payload={"action_id": "act_ok"})
    ledger.record(LedgerEntryType.COMMIT_SUCCEEDED, actor=EXECUTOR, payload={"action_id": "act_rogue"})
    assert ledger.commits_without_consumed_token() == ["act_rogue"]


def test_ledger_refuses_raw_content_payloads() -> None:
    with pytest.raises(ValueError, match="ids and hashes"):
        Ledger(InMemoryLedgerStore()).record(LedgerEntryType.DRAFT_SAVED, actor=AGENT, payload={"text": "x" * 5000})


# --------------------------------------------------------------------------------------------------- tokens
def _claims(**overrides: object) -> ApprovalTokenClaims:
    now = int(time.time())
    data: dict[str, object] = {"iss": "https://ledger.test", "sub": "pm-1", "aud": "commit-executor", "jti": "tok_1",
                               "iat": now, "exp": now + 900, "act": "act_1", "pah": content_sha256(PAYLOAD),
                               "tgt": {"system": "ado"}, "scp": [SCOPE], "vrf": "sha256:abc"}
    data.update(overrides)
    return ApprovalTokenClaims.model_validate(data)


def _verify(token: str, signer: LocalEcdsaSigner, **overrides: object) -> ApprovalTokenClaims:
    kwargs: dict[str, object] = {"keys": {signer.key_id: signer.public_key_pem()}, "audience": "commit-executor",
                                 "issuer": "https://ledger.test", "scope": SCOPE, "payload": PAYLOAD,
                                 "action_id": "act_1"}
    kwargs.update(overrides)
    return verify_token(token, **kwargs)  # type: ignore[arg-type]


def test_token_round_trip_binds_payload_scope_audience_and_action() -> None:
    signer = LocalEcdsaSigner(key_id="approval-1")
    claims = _verify(mint_token(_claims(), signer), signer)
    assert claims.act == "act_1" and claims.scp == [SCOPE]


@pytest.mark.parametrize(("overrides", "code"), [
    ({"payload": PAYLOAD | {"fields": {"System.Title": "edited after approval"}}}, "payload_mismatch"),
    ({"scope": "kb_write.publish_article"}, "scope"),
    ({"audience": "mcp-kb-write"}, "wrong_audience"),
    ({"issuer": "https://evil.test"}, "wrong_issuer"),
    ({"action_id": "act_other"}, "action_mismatch"),
])
def test_token_misuse_is_refused(overrides: dict[str, object], code: str) -> None:
    signer = LocalEcdsaSigner(key_id="approval-1")
    with pytest.raises(TokenError) as error:
        _verify(mint_token(_claims(), signer), signer, **overrides)
    assert error.value.code == code


def test_expired_token_is_refused() -> None:
    signer = LocalEcdsaSigner(key_id="approval-1")
    now = int(time.time())
    with pytest.raises(TokenError) as error:
        _verify(mint_token(_claims(iat=now - 2000, exp=now - 1000), signer), signer)
    assert error.value.code == "expired"


def test_token_signed_by_another_key_is_refused() -> None:
    signer, rogue = LocalEcdsaSigner(key_id="approval-1"), LocalEcdsaSigner(key_id="approval-1")
    with pytest.raises(TokenError) as error:
        _verify(mint_token(_claims(), rogue), signer)
    assert error.value.code == "bad_signature"
    with pytest.raises(TokenError) as unknown:
        _verify(mint_token(_claims(), LocalEcdsaSigner(key_id="unknown-kid")), signer)
    assert unknown.value.code == "unknown_key"


def test_alg_none_and_tampered_tokens_are_refused() -> None:
    signer = LocalEcdsaSigner(key_id="approval-1")
    header = b64url_encode(json.dumps({"alg": "none", "kid": "approval-1"}).encode())
    body = b64url_encode(_claims().model_dump_json().encode())
    with pytest.raises(TokenError) as none_alg:
        _verify(f"{header}.{body}.", signer)
    assert none_alg.value.code == "unsupported_alg"
    good = mint_token(_claims(), signer)
    head, _, sig = good.split(".")
    forged_body = b64url_encode(_claims(scp=["ado_write.create_work_item", "kb_write.publish_article"])
                                .model_dump_json().encode())
    with pytest.raises(TokenError) as tampered:
        _verify(f"{head}.{forged_body}.{sig}", signer)
    assert tampered.value.code == "bad_signature"
    with pytest.raises(TokenError) as garbage:
        _verify("not-a-token", signer)
    assert garbage.value.code == "malformed"


# -------------------------------------------------------------------------------------------------- catalog
def test_catalog_maps_every_action_to_one_write_tool_and_approver_rule() -> None:
    catalog = ActionCatalog.from_settings(local_settings())
    spec = catalog.get(ActionType.ADO_CREATE_WORK_ITEM.value)
    assert (spec.server, spec.tool, spec.scope) == ("mcp-ado-write", "create_work_item", SCOPE)
    assert spec.subject_types == frozenset({"requirement_draft"})
    assert {s.action_type for s in catalog.specs()} == set(ActionType)
    figma = catalog.get(ActionType.FIGMA_RENDER_PROTOTYPE.value)
    assert figma.min_approvals == 2 and figma.require_each_group
    assert "create_work_item" in catalog.write_tools()
    with pytest.raises(UnknownAction):
        catalog.get("DELETE_PROJECT")


# --------------------------------------------------------------------------------------- atomic batches (S2)
def _batch() -> list[NewEntry]:
    return [NewEntry(LedgerEntryType.TOKEN_CONSUMED, EXECUTOR, {"action_id": "act_1", "jti": "tok_1"}, trace_id="t1"),
            NewEntry(LedgerEntryType.COMMIT_STARTED, EXECUTOR, {"action_id": "act_1"}, trace_id="t1")]


@pytest.mark.parametrize("store_factory", [InMemoryLedgerStore, lambda: SqliteLedgerStore(":memory:")])
def test_record_many_appends_one_chained_batch(store_factory) -> None:  # type: ignore[no-untyped-def]
    ledger = Ledger(store_factory())
    first = ledger.record(LedgerEntryType.ACTION_PROPOSED, actor=AGENT, subject_refs=[PROBLEM],
                          payload={"action_id": "act_1"})
    consumed, started = ledger.record_many(_batch())
    assert [first.seq, consumed.seq, started.seq] == [1, 2, 3]
    assert consumed.prev_hash == first.entry_hash and started.prev_hash == consumed.entry_hash
    assert (consumed.trace_id, started.payload) == ("t1", {"action_id": "act_1"})
    assert ledger.verify_chain().ok and ledger.record_many([]) == []
    assert [e.entry_type for e in ledger.query(action_id="act_1")] == [
        LedgerEntryType.ACTION_PROPOSED, LedgerEntryType.TOKEN_CONSUMED, LedgerEntryType.COMMIT_STARTED]


def test_record_many_checks_every_payload_before_writing_any() -> None:
    ledger = Ledger(InMemoryLedgerStore())
    oversized = NewEntry(LedgerEntryType.COMMIT_STARTED, EXECUTOR, {"action_id": "act_1", "text": "x" * 600})
    with pytest.raises(ValueError, match="ids and hashes"):
        ledger.record_many([_batch()[0], oversized])
    assert ledger.head() is None


@pytest.mark.parametrize("store_factory", [InMemoryLedgerStore, lambda: SqliteLedgerStore(":memory:")])
def test_store_append_many_is_all_or_nothing(store_factory) -> None:  # type: ignore[no-untyped-def]
    source = Ledger(InMemoryLedgerStore())
    first, second, third = source.record_many(_batch() + [_batch()[1]])
    store = store_factory()
    with pytest.raises(ValueError, match="gap"):
        store.append_many([first, third])  # seq 1 then 3: the valid first entry must not be kept
    assert store.head() is None and list(store.entries()) == []
    store.append_many([first, second])
    store.append_many([third])
    assert [e.seq for e in store.entries()] == [1, 2, 3] and Ledger(store).verify_chain().ok


def test_sqlite_batch_survives_reopen_and_rolls_back_on_failure(tmp_path) -> None:  # type: ignore[no-untyped-def]
    path = tmp_path / "ledger.db"
    ledger = Ledger(SqliteLedgerStore(path))
    ledger.record_many(_batch())
    broken = SqliteLedgerStore(path)
    entries = list(broken.entries())
    with pytest.raises(sqlite3.IntegrityError):
        broken.append_many([entries[1].model_copy(update={"seq": 3, "entry_id": "led_new"}),
                            entries[1].model_copy(update={"seq": 4})])  # duplicate entry_id: UNIQUE fails
    reopened = Ledger(SqliteLedgerStore(path))
    assert [e.seq for e in reopened.query()] == [1, 2] and reopened.verify_chain().ok
