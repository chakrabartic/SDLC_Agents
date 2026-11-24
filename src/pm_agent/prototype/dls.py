"""SLB Design Language System catalog snapshot (docs/09 §6, FR-B2/B3).

The ``dls-catalog-sync`` job snapshots the published Figma library (keys, names, variant properties, publish
version); prototype specs may only instantiate keys from the pinned snapshot (V9). The repository ships a synthetic
sample until SLB supplies the library (Q-03).
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

SAMPLE_CATALOG = Path(__file__).resolve().parents[3] / "config" / "dls_catalog.sample.json"


class DlsComponent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key: str
    name: str
    description: str = ""
    variants: dict[str, list[str]] = Field(default_factory=dict)


class DlsCatalog(BaseModel):
    model_config = ConfigDict(extra="ignore")

    file_key: str
    published_version: str
    components: list[DlsComponent]

    @property
    def keys(self) -> frozenset[str]:
        return frozenset(c.key for c in self.components)

    def get(self, key: str) -> DlsComponent | None:
        return next((c for c in self.components if c.key == key), None)

    def for_prompt(self) -> list[dict[str, object]]:
        """Compact listing given to the prototype composer (keys must be copied exactly)."""
        return [{"component_key": c.key, "name": c.name, "description": c.description, "variants": c.variants}
                for c in self.components]


def load_dls_catalog(path: Path | None = None) -> DlsCatalog:
    """The pinned snapshot: *path*, else ``PM_AGENT_DLS_SNAPSHOT`` (container images), else the repo sample."""
    env_path = os.environ.get("PM_AGENT_DLS_SNAPSHOT")
    with (path or (Path(env_path) if env_path else SAMPLE_CATALOG)).open(encoding="utf-8") as handle:
        return DlsCatalog.model_validate(json.load(handle))
