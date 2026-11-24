"""Scheduled jobs (GKE CronJobs, docs/12): ledger anchoring, DLS catalog sync, correlation re-clustering.

Each job is a ``main() -> int`` entry point dispatched by ``pm_agent.serve`` and emits one structured JSON log line
on stdout — on GKE that line lands in Cloud Logging, where a sink routes it (e.g. ledger anchors to the locked
``pm-agent-ledger`` bucket, Terraform ``logging`` module).
"""

from __future__ import annotations

import json
import sys
from typing import Any


def emit(severity: str, **fields: Any) -> dict[str, Any]:
    """Write one structured log line (Cloud Logging ``jsonPayload``) and return it."""
    record = {"severity": severity, **fields}
    sys.stdout.write(json.dumps(record, sort_keys=True, default=str) + "\n")
    return record
