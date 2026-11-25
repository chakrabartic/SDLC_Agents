"""CI complexity budget: fail on any function whose cyclomatic complexity exceeds the budget without a waiver.

Run: python tools/complexity_budget.py   (exit code 1 on violations or stale waivers)

Budget and waivers live in ``pyproject.toml`` under ``[tool.complexity-budget]`` so exceptions are reviewed like
code: each waiver names one function and says *why* splitting it would add accidental complexity. A waiver that no
longer exceeds the budget is reported as stale, so the list only shrinks.
"""

from __future__ import annotations

import sys
import tomllib
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path

from radon.complexity import cc_visit
from radon.visitors import Class, Function

ROOT = Path(__file__).resolve().parents[1]


@dataclass(frozen=True)
class Measured:
    qualname: str
    complexity: int
    location: str


def _blocks(blocks: Iterable[Function | Class], prefix: str) -> Iterator[tuple[str, Function]]:
    for block in blocks:
        if isinstance(block, Class):
            yield from _blocks(block.methods, f"{prefix}.{block.name}")
        else:
            yield f"{prefix}.{block.name}", block
            yield from _blocks(block.closures, f"{prefix}.{block.name}")


def measure(paths: Iterable[str]) -> list[Measured]:
    """Every function, method and closure under *paths*, keyed by dotted module path + qualified name."""
    found: list[Measured] = []
    for base in paths:
        for path in sorted((ROOT / base).rglob("*.py")):
            rel = path.relative_to(ROOT)
            parts = rel.with_suffix("").parts
            module = ".".join(parts[1:] if parts[0] == "src" else parts)
            for qualname, block in _blocks(cc_visit(path.read_text(encoding="utf-8")), module):
                found.append(Measured(qualname, block.complexity, f"{rel.as_posix()}:{block.lineno}"))
    return found


def check(config: dict[str, object]) -> list[str]:
    """Violations (over budget without a waiver) and stale waivers (waived but back within budget)."""
    budget = int(str(config.get("max_cc", 15)))
    waivers = dict(config.get("waivers", {}))  # type: ignore[call-overload]
    paths = list(config.get("paths", ["src", "tools"]))  # type: ignore[call-overload]
    problems: list[str] = []
    over = {m.qualname: m for m in measure(paths) if m.complexity > budget}
    for name, item in sorted(over.items()):
        if name not in waivers:
            problems.append(f"{item.location}: {name} has cyclomatic complexity {item.complexity} > {budget} "
                            "(split it, or add a justified waiver to [tool.complexity-budget.waivers])")
    for name in sorted(set(waivers) - set(over)):
        problems.append(f"stale waiver: {name} is within budget now — remove it from pyproject.toml")
    return problems


def main() -> int:
    config = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    section = config.get("tool", {}).get("complexity-budget", {})
    problems = check(section)
    for problem in problems:
        print(problem)
    print(f"complexity budget: {len(problems)} problem(s) (max cc {section.get('max_cc', 15)})")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
