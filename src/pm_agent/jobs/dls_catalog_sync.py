"""``dls-catalog-sync`` CronJob (docs/09 §6): snapshot the published SLB DLS Figma library.

Production reads the library with a user-less plan access token (``GET /v1/files/{key}/components`` and
``component_sets``) and writes a versioned snapshot that the Verifier pins (V9). Until SLB supplies the library and
token (Q-03), the job validates the configured snapshot and reports it.
"""

from __future__ import annotations

import os
from pathlib import Path

from pm_agent.jobs import emit
from pm_agent.placeholders import is_placeholder, not_implemented
from pm_agent.prototype.dls import DlsCatalog, load_dls_catalog
from pm_agent.settings import load_settings


def fetch_library(file_key: str) -> DlsCatalog:
    """Figma REST read of the published library (plan access token) — awaiting Q-03."""
    del file_key
    not_implemented("Q-03", "Figma library snapshot (components, component sets, variant properties)")


def main() -> int:
    """Validate (and in production refresh) the pinned DLS snapshot; non-zero exit on duplicate keys."""
    settings = load_settings()
    snapshot = Path(os.environ.get("PM_AGENT_DLS_SNAPSHOT", "")) if os.environ.get("PM_AGENT_DLS_SNAPSHOT") else None
    file_key = str(settings.connectors.get("figma", {}).get("dls_library_file_key", ""))
    catalog = load_dls_catalog(snapshot) if is_placeholder(file_key) or not settings.strict else fetch_library(file_key)
    duplicates = len(catalog.components) - len(catalog.keys)
    emit("INFO" if not duplicates else "ERROR", job="dls-catalog-sync", components=len(catalog.components),
         published_version=catalog.published_version, duplicate_keys=duplicates,
         sample=is_placeholder(catalog.file_key))
    return 1 if duplicates else 0
