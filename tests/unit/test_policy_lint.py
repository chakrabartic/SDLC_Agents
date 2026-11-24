"""The CI policy lint (gate 1) catches forbidden APIs and LLM-side access to signing/write paths."""
from __future__ import annotations

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location("policy_lint", ROOT / "tools" / "policy_lint.py")
assert _spec and _spec.loader
policy_lint = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(policy_lint)


def _hits(rel: str, line: str) -> list[str]:
    found = [rid for rid, pattern, _, allowed in policy_lint.RULES if pattern.search(line) and rel not in allowed]
    found += [rid for rid, pattern, _, prefixes in policy_lint.SCOPED_RULES
              if rel.startswith(prefixes) and pattern.search(line)]
    return found


def test_repository_is_clean() -> None:
    assert policy_lint.scan() == []


def test_llm_side_modules_cannot_mint_tokens_reach_write_servers_or_decide() -> None:
    assert _hits("src/pm_agent/workflows/x.py", "token = mint_token(claims, signer)") == ["PL007"]
    assert _hits("src/pm_agent/core_app/x.py", "server = WriteToolServer(name, guard, handlers)") == ["PL008"]
    assert _hits("src/pm_agent/agents/x.py", "services.approvals.decide(action_id=a, decision=d)") == ["PL009"]
    assert _hits("src/pm_agent/hitl/approvals.py", "token = mint_token(claims, signer)") == []  # ledger may sign


def test_forbidden_sdk_and_keys_are_flagged_everywhere() -> None:
    assert _hits("src/pm_agent/x.py", "import google.generativeai as genai") == ["PL001"]
    assert _hits("src/pm_agent/x.py", 'os.environ["GOOGLE_API_KEY"]') == ["PL002"]
    assert _hits("src/pm_agent/x.py", "client = genai.Client(vertexai=True)") == ["PL006"]
