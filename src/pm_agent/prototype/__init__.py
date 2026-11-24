"""Prototype (UX) capability support: DLS catalog snapshot and the Figma Plugin API compiler (docs/04 A5)."""

from pm_agent.prototype.compile import compile_plugin_program
from pm_agent.prototype.dls import DlsCatalog, DlsComponent, load_dls_catalog

__all__ = ["DlsCatalog", "DlsComponent", "compile_plugin_program", "load_dls_catalog"]
