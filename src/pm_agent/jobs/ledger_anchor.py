"""``ledger-anchor`` CronJob (docs/07 §8.1): verify the hash chain and anchor its head every 5 minutes.

Runs as the ledger service account (Workload Identity). The anchor is a structured log line routed by a sink to the
locked Cloud Logging bucket; a chain mismatch logs ``CRITICAL`` (SEV-1 alert policy) and exits non-zero.
Production reads ``hitl.ledger_entries`` from AlloyDB (placeholder until the stage project exists, Q-10).
"""

from __future__ import annotations

import os

from pm_agent.hitl.ledger import Ledger, LedgerStore, SqliteLedgerStore
from pm_agent.jobs import emit
from pm_agent.placeholders import not_implemented
from pm_agent.settings import load_settings

ANCHOR_TYPE = "pm-agent-ledger-anchor"


def open_store() -> LedgerStore:
    """Local/dev: SQLite file from ``PM_AGENT_LEDGER_PATH``; stage/prod: AlloyDB (Q-10)."""
    settings = load_settings()
    path = os.environ.get("PM_AGENT_LEDGER_PATH")
    if not settings.strict and path:
        return SqliteLedgerStore(path)
    not_implemented("Q-10", "AlloyDB ledger store (hitl.ledger_entries, INSERT/SELECT role) for the anchor job")


def anchor(ledger: Ledger) -> int:
    """Verify from genesis and emit the anchor record; returns the process exit code."""
    result = ledger.verify_chain()
    head = ledger.head()
    if not result.ok:
        emit("CRITICAL", anchor_type=ANCHOR_TYPE, verified=False, first_bad_seq=result.first_bad_seq,
             checked=result.checked)
        return 2
    emit("NOTICE", anchor_type=ANCHOR_TYPE, verified=True, checked=result.checked,
         seq=head.seq if head else 0, entry_hash=head.entry_hash if head else None)
    return 0


def main() -> int:
    return anchor(Ledger(open_store()))
