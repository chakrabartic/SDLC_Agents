"""A2UI v1.0 adapter for the PM Workbench (docs/11) — the only module that talks to the A2UI SDKs.

* ``pm_agent.a2ui`` — protocol constants, renderer-capability negotiation, ids, A2A packaging, the SLB catalog.
* ``pm_agent.a2ui.surfaces`` — deterministic surface builders over verified objects (no LLM in the loop).
* ``pm_agent.a2ui.actions`` — strict parsing of renderer→agent ``action`` messages; decision events.
"""

from pm_agent.a2ui._core import (
    A2UI_VERSION,
    BASIC_CATALOG_ID,
    EXTENSION_URI,
    MIME_TYPE,
    RENDERER_CAPABILITIES_KEY,
    SLB_CATALOG_ID,
    SUPPORTED_CATALOG_IDS,
    agent_extension,
    load_slb_catalog,
    renderer_capabilities,
    sanitize_id,
    supports_a2ui,
    to_a2a_parts,
)

__all__ = [
    "A2UI_VERSION", "BASIC_CATALOG_ID", "EXTENSION_URI", "MIME_TYPE", "RENDERER_CAPABILITIES_KEY", "SLB_CATALOG_ID",
    "SUPPORTED_CATALOG_IDS", "agent_extension", "load_slb_catalog", "renderer_capabilities", "sanitize_id",
    "supports_a2ui", "to_a2a_parts",
]
