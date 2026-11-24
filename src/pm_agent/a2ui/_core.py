"""Protocol constants, capability negotiation, ids and A2A packaging for A2UI v1.0 (docs/11 §1–§4).

Everything version- or SDK-specific is resolved here once, so SDK churn touches one module (ADR-007). Verified
against the installed ``a2ui-agent-sdk`` 0.12.1 / ``a2ui-a2a`` 0.4.0 and the cached v1.0 specification.
"""

from __future__ import annotations

import functools
import importlib.resources
import json
import re
from collections.abc import Iterable, Mapping, Sequence
from typing import Any, Final

from a2a.types import AgentExtension, Part
from a2ui.a2a import A2UI_MIME_TYPE, create_a2ui_parts, get_a2ui_agent_extension, get_a2ui_extension_uri
from a2ui.core.basic_catalog import BasicCatalog
from a2ui.utils import read_renderer_capabilities

A2UI_VERSION: Final = "1.0"
#: Basic catalog id, read from the SDK (never hard-coded elsewhere):
#: ``https://a2ui.org/specification/v1_0/catalogs/basic/catalog.json``.
BASIC_CATALOG_ID: Final[str] = BasicCatalog(A2UI_VERSION).catalog_id
#: Our domain catalog — domain-prefixed, major version in the id, additive-only evolution (docs/11 §4).
SLB_CATALOG_ID: Final = "slb.com:pm_workbench_v1"
#: ``https://a2ui.org/a2a-extension/a2ui/v1.0`` (activation is optional; capabilities are what count).
EXTENSION_URI: Final[str] = get_a2ui_extension_uri(A2UI_VERSION)
#: ``application/a2ui+json`` — carried in ``DataPart.metadata["mimeType"]``.
MIME_TYPE: Final[str] = A2UI_MIME_TYPE
#: Key of the version-keyed renderer capabilities in A2A ``message.metadata``.
RENDERER_CAPABILITIES_KEY: Final = "a2uiRendererCapabilities"
SUPPORTED_CATALOG_IDS: Final[tuple[str, ...]] = (SLB_CATALOG_ID, BASIC_CATALOG_ID)

_CATALOG_RESOURCE: Final = "catalogs/slb_pm_workbench_v1.json"
_NOT_ID_CHAR = re.compile(r"[^A-Za-z0-9_]")


def renderer_capabilities() -> dict[str, Any]:
    """Version-keyed capabilities the PM Workbench BFF attaches to **every** A2A message.

    ``message.metadata["a2uiRendererCapabilities"] = renderer_capabilities()`` →
    ``{"1.0": {"supportedCatalogIds": ["slb.com:pm_workbench_v1", "<basic catalog id>"]}}``. A fresh dict per call.
    """
    return {A2UI_VERSION: {"supportedCatalogIds": list(SUPPORTED_CATALOG_IDS)}}


def supports_a2ui(metadata: Mapping[str, Any] | None, *, catalogs: Iterable[str] = SUPPORTED_CATALOG_IDS) -> bool:
    """True only if this turn's A2A ``message.metadata`` offers A2UI v1.0 with every catalog in *catalogs*.

    Per the A2A extension spec (capability scoping) an agent MUST NOT send ``application/a2ui+json`` parts on a
    turn without v1.0 renderer capabilities; callers fall back to text. Fails closed: absent, flat (unkeyed),
    other-version or malformed capabilities all return ``False`` — this function never raises.
    """
    if not isinstance(metadata, Mapping):
        return False
    try:
        capabilities = read_renderer_capabilities(metadata, A2UI_VERSION)
    except (ValueError, TypeError):  # A2uiCatalogError is a ValueError: malformed capabilities ⇒ no A2UI
        return False
    if not capabilities:
        return False
    entry = capabilities.get(A2UI_VERSION)
    offered = entry.get("supportedCatalogIds") if isinstance(entry, Mapping) else None
    if not isinstance(offered, list):
        return False
    return set(catalogs) <= {c for c in offered if isinstance(c, str)}


def sanitize_id(text: str) -> str:
    """Deterministic ASCII UAX #31 identifier for a component or surface id (``approve-act-1`` → ``approve_act_1``).

    Every character outside ``[A-Za-z0-9_]`` becomes ``_`` and a leading digit gets a ``_`` prefix, so the result
    matches ``^[\\p{XID_Start}_][\\p{XID_Continue}]*$`` on every renderer. Idempotent; ``""`` becomes ``"_"``.
    """
    clean = _NOT_ID_CHAR.sub("_", text)
    if not clean or clean[0].isdigit():
        clean = "_" + clean
    return clean


def to_a2a_parts(messages: Sequence[Mapping[str, Any]]) -> list[Part]:
    """Wrap A2UI v1.0 messages as A2A ``DataPart``s (one per message, ``metadata.mimeType = application/a2ui+json``).

    Raises ``ValueError`` for anything that is not a v1.0 message, so a stray v0.9 payload never reaches the BFF.
    UNCONFIRMED spec/SDK gap: the v1.0 A2A extension text says ``DataPart.data`` "MUST be an array of messages",
    but ``a2a-sdk`` 0.3.x types ``DataPart.data`` as an object and ``a2ui-a2a`` emits one message per part.
    """
    for index, message in enumerate(messages):
        if not isinstance(message, Mapping) or message.get("version") != A2UI_VERSION:
            raise ValueError(f"message {index} is not an A2UI {A2UI_VERSION} message")
    return create_a2ui_parts([dict(m) for m in messages], version=A2UI_VERSION)


def agent_extension() -> AgentExtension:
    """AgentCard ``capabilities.extensions`` entry advertising A2UI v1.0 with the catalogs we generate."""
    return get_a2ui_agent_extension(A2UI_VERSION, supported_catalog_ids=list(SUPPORTED_CATALOG_IDS))


@functools.cache
def _catalog_text() -> str:
    return importlib.resources.files("pm_agent.a2ui").joinpath(_CATALOG_RESOURCE).read_text(encoding="utf-8")


def load_slb_catalog() -> dict[str, Any]:
    """The ``slb.com:pm_workbench_v1`` catalog (JSON Schema, ``$id == catalogId``) from package data; a fresh copy."""
    catalog: dict[str, Any] = json.loads(_catalog_text())
    return catalog
