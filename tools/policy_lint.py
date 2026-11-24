"""CI policy lint (docs/12 §4 stage 2): fail the build on patterns the architecture forbids.

Run: python tools/policy_lint.py   (exit code 1 on violations)
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCAN_DIRS = ("src", "tools", "deploy")
SELF = Path(__file__).resolve()

# (rule id, regex, explanation, files allowed to contain it)
RULES: list[tuple[str, re.Pattern[str], str, tuple[str, ...]]] = [
    ("PL001", re.compile(r"google\.generativeai|import\s+google\.generativeai"),
     "Legacy SDK forbidden — use google-genai (GEMINI.md).", ()),
    ("PL002", re.compile(r"GOOGLE_API_KEY|GEMINI_API_KEY"),
     "API keys forbidden — use enterprise=True + ADC (GEMINI.md).", ("src/pm_agent/settings.py",)),
    ("PL003", re.compile(r"except\s+BaseException"),
     "Never catch BaseException: it traps ADK NodeInterruptedError (ADK 2.0 migration notes).", ()),
    ("PL004", re.compile(r"session\.events\.append|enqueue_event\("),
     "Do not append events manually; yield them (ADK 2.0 migration notes).", ()),
    ("PL005", re.compile(r"_run_async_impl\s*\("),
     "Custom _run_async_impl overrides are bypassed by the ADK 2.x Workflow engine.", ()),
    ("PL006", re.compile(r"vertexai\s*=\s*True"),
     "Use enterprise=True (vertexai is the legacy flag).", ()),
]

# Architecture rules (ADR-003, docs/07 H-1/H-3): code that hosts LLM nodes never touches signing material, write
# servers or approval decisions. (rule id, regex, explanation, path prefixes the rule applies to)
LLM_SIDE = ("src/pm_agent/agents/", "src/pm_agent/workflows/", "src/pm_agent/core_app/", "src/pm_agent/ingestion_app/")
SCOPED_RULES: list[tuple[str, re.Pattern[str], str, tuple[str, ...]]] = [
    ("PL007", re.compile(r"\bmint_token\b|\bLocalEcdsaSigner\b|\bKmsSigner\b|token_signer"),
     "LLM-side modules may not hold or use signing material (H-3).", LLM_SIDE),
    ("PL008", re.compile(r"\bWriteToolServer\b|\bRecordingWriteBackend\b|\bWriteToolGuard\b|\bconsume_token\b"),
     "LLM-side modules may not reach write servers or consume tokens; use services.commit (A2A) (H-1).", LLM_SIDE),
    ("PL009", re.compile(r"approvals\.decide\(|\.decide\(action_id"),
     "Only the workbench BFF records PM decisions — never an agent workflow (H-2).", LLM_SIDE),
]


def scan() -> list[str]:
    """Return ``path:line: rule message`` for every forbidden pattern found in the scanned directories."""
    violations: list[str] = []
    for directory in SCAN_DIRS:
        base = ROOT / directory
        if not base.exists():
            continue
        for path in sorted(base.rglob("*.py")):
            if path.resolve() == SELF:
                continue
            rel = path.relative_to(ROOT).as_posix()
            for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
                for rule_id, pattern, why, allowed in RULES:
                    if pattern.search(line) and rel not in allowed:
                        violations.append(f"{rel}:{lineno}: {rule_id} {why}")
                for rule_id, pattern, why, prefixes in SCOPED_RULES:
                    if rel.startswith(prefixes) and pattern.search(line):
                        violations.append(f"{rel}:{lineno}: {rule_id} {why}")
    return violations


def main() -> int:
    violations = scan()
    for violation in violations:
        print(violation)
    print(f"policy lint: {len(violations)} violation(s)")
    return 1 if violations else 0


if __name__ == "__main__":
    sys.exit(main())
