"""Architecture fitness tests — the layering rules of ADR-003 / ADR-011 as executable checks.

These guard structure, not behaviour, so regressions are caught at review time instead of in production:

* no import cycles between ``pm_agent`` sub-packages;
* ``pm_agent.schemas`` is the leaf every layer may depend on, and depends on nothing else in ``pm_agent``;
* only deployable entry points import the composition root (``pm_agent.runtime``);
* LLM-side code (agents, workflows, the core and ingestion apps) never *loads* write-side or token-signing modules —
  checked on the real import closure in a fresh interpreter, so transitive and package ``__init__`` imports count.
"""

from __future__ import annotations

import ast
import json
import subprocess
import sys
from collections import defaultdict
from pathlib import Path

import pytest

pytestmark = pytest.mark.architecture

SRC = Path(__file__).resolve().parents[2] / "src"
PACKAGE = "pm_agent"

COMPOSITION_ROOT = "pm_agent.runtime"
# Deployable entry points: Agent Runtime loaders (``<pkg>:app``) and ASGI factories that wire from the environment.
ENTRY_POINTS = frozenset({"pm_agent.core_app", "pm_agent.ingestion_app", "pm_agent.executor_app",
                          "pm_agent.workbench.app"})

LLM_SIDE_PACKAGES = ("pm_agent.agents", "pm_agent.workflows")
LLM_SIDE_MODULES = ("pm_agent.ports", "pm_agent.core_app.agent", "pm_agent.ingestion_app.agent")
# ADR-003 / docs/07 H-1, H-3: code that hosts LLM nodes never loads token minting, write servers, the commit executor
# or the composition root that wires them.
WRITE_SIDE = frozenset({
    COMPOSITION_ROOT,
    "pm_agent.hitl.approvals",
    "pm_agent.hitl.tokens",
    "pm_agent.hitl.write_guard",
    "pm_agent.hitl.executor",
    "pm_agent.mcp_servers.write_server",
    "pm_agent.mcp_servers.write_handlers",
    "pm_agent.connectors.recording",
    "pm_agent.renderer.app",
    "pm_agent.renderer.backend",
})


# --------------------------------------------------------------------------------------------- static graph
def _module_name(path: Path) -> str:
    parts = list(path.relative_to(SRC).with_suffix("").parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def _type_checking_nodes(tree: ast.Module) -> set[int]:
    """Ids of nodes inside ``if TYPE_CHECKING:`` blocks (type-only imports create no runtime coupling)."""
    hidden: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.If) and "TYPE_CHECKING" in ast.unparse(node.test):
            hidden.update(id(child) for stmt in node.body for child in ast.walk(stmt))
    return hidden


def _imports(path: Path, module: str) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    hidden = _type_checking_nodes(tree)
    is_package = path.name == "__init__.py"
    found: set[str] = set()
    for node in ast.walk(tree):
        if id(node) in hidden:
            continue
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                base = module.split(".") if is_package else module.split(".")[:-1]
                base = base[: len(base) - (node.level - 1)]
                found.add(".".join([*base, node.module] if node.module else base))
            elif node.module:
                found.add(node.module)
    return {name for name in found if name == PACKAGE or name.startswith(PACKAGE + ".")}


def _graph() -> dict[str, set[str]]:
    return {_module_name(p): _imports(p, _module_name(p)) for p in sorted((SRC / PACKAGE).rglob("*.py"))}


def _subpackage(module: str) -> str:
    parts = module.split(".")
    return parts[1] if len(parts) > 1 else "<root>"


def _strongly_connected(graph: dict[str, set[str]]) -> list[set[str]]:
    index: dict[str, int] = {}
    low: dict[str, int] = {}
    stack: list[str] = []
    on_stack: set[str] = set()
    components: list[set[str]] = []

    def visit(node: str) -> None:
        index[node] = low[node] = len(index)
        stack.append(node)
        on_stack.add(node)
        for succ in sorted(graph.get(node, ())):
            if succ not in index:
                visit(succ)
                low[node] = min(low[node], low[succ])
            elif succ in on_stack:
                low[node] = min(low[node], index[succ])
        if low[node] == index[node]:
            component: set[str] = set()
            while True:
                member = stack.pop()
                on_stack.discard(member)
                component.add(member)
                if member == node:
                    break
            components.append(component)

    for node in sorted(set(graph) | {d for deps in graph.values() for d in deps}):
        if node not in index:
            visit(node)
    return components


# --------------------------------------------------------------------------------------------------- tests
def test_no_import_cycles_between_subpackages() -> None:
    packages: dict[str, set[str]] = defaultdict(set)
    for module, deps in _graph().items():
        for dep in deps:
            if _subpackage(dep) != _subpackage(module):
                packages[_subpackage(module)].add(_subpackage(dep))
    cycles = [sorted(c) for c in _strongly_connected(packages) if len(c) > 1]
    assert not cycles, f"sub-package import cycles: {cycles}"


def test_schemas_is_a_leaf() -> None:
    leaks = {module: sorted(d for d in deps if not d.startswith("pm_agent.schemas"))
             for module, deps in _graph().items() if module.startswith("pm_agent.schemas")}
    assert not {m: d for m, d in leaks.items() if d}, "pm_agent.schemas must not import other pm_agent packages"


def test_only_entry_points_import_the_composition_root() -> None:
    importers = {module for module, deps in _graph().items() if COMPOSITION_ROOT in deps}
    assert importers <= ENTRY_POINTS, f"composition root imported by non-entry points: {sorted(importers - ENTRY_POINTS)}"


def _llm_side_modules() -> list[str]:
    modules = [m for m in _graph() if m.startswith(LLM_SIDE_PACKAGES)]
    return sorted({*modules, *LLM_SIDE_MODULES})


_PROBE = """
import importlib, json, sys
blame = {}
for name in json.loads(sys.argv[1]):
    before = set(sys.modules)
    importlib.import_module(name)
    for loaded in sorted(set(sys.modules) - before):
        blame.setdefault(loaded, name)
print(json.dumps(blame))
"""


def test_llm_side_code_never_loads_write_side_modules() -> None:
    """Runtime import closure in a fresh interpreter (transitive and package ``__init__`` imports included)."""
    probe = subprocess.run([sys.executable, "-c", _PROBE, json.dumps(_llm_side_modules())], capture_output=True,
                           text=True, check=True, env={"PYTHONPATH": str(SRC), "PM_AGENT_ENV": "local"})
    first_importer: dict[str, str] = json.loads(probe.stdout.strip().splitlines()[-1])
    violations = {module: first_importer[module] for module in sorted(WRITE_SIDE & set(first_importer))}
    assert not violations, f"LLM-side code loads write-side modules (module -> first LLM-side importer): {violations}"
