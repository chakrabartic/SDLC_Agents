"""Offline evaluation baseline (docs/13 §3): synthetic golden signals → W1/W2 → W3 up to the approval pause.

No credentials, no network: deterministic extractive model, in-process services. Scores E1 (problem formation),
E3 (groundedness — hard gate), E7 (redaction) and prints a JSON report; exits 1 when a hard gate fails.
Nothing is ever approved or written by this script.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any

from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

# pylint: disable=wrong-import-position
from pm_agent.agents.offline import ExtractiveModelProvider  # noqa: E402
from pm_agent.connectors.fixtures import golden_connectors, load_golden_manifest  # noqa: E402
from pm_agent.core_app import build_core_app  # noqa: E402
from pm_agent.grounding import LocalRedactor, iter_claims  # noqa: E402
from pm_agent.grounding.redaction import SECRET_TYPES  # noqa: E402
from pm_agent.hitl.resume import pending_interrupts, resume_content  # noqa: E402
from pm_agent.ingestion_app import build_ingestion_app  # noqa: E402
from pm_agent.runtime import build_local_services  # noqa: E402
from pm_agent.schemas import CITATION_REQUIRED, Approver  # noqa: E402
from pm_agent.settings import load_settings  # noqa: E402

EVAL_PM = Approver(sub="eval-pm@slb.test", email="EMAIL_EVAL01", idp="local", groups=["pm_approvers"])
CANNED_ANSWERS = {
    "who_affected": "Affected users are the drilling data engineers who rely on the standardized logs.",
    "current_vs_desired_workflow": "Currently the channel is missing; expected workflow: it appears without manual steps.",
    "should_do_should_not": "The system must populate the channel and should not require a manual re-run.",
    "acceptance_criteria": "Acceptance criteria: the channel appears in the standardized log within 5 minutes.",
    "edge_cases": "Edge case: the mapping file arrives late or is missing.",
    "dependencies_constraints": "Depends on the mapping file being available before streaming starts.",
}


async def _run(runner: Runner, user: str, session_id: str, message: types.Content,
               invocation_id: str | None = None) -> tuple[list[Any], str | None]:
    events, current = [], invocation_id
    async for event in runner.run_async(user_id=user, session_id=session_id, new_message=message,
                                        invocation_id=invocation_id):
        events.append(event)
        current = event.invocation_id or current
    return events, current


async def evaluate() -> dict[str, Any]:
    """Run the baseline and return the metrics report."""
    settings = load_settings(environ={"PM_AGENT_ENV": "local"})
    redactor = LocalRedactor.from_settings(settings, pseudonym_key=b"eval-offline")
    services = build_local_services(settings, connectors=golden_connectors(redactor))
    provider = ExtractiveModelProvider()
    await _ingest(services, provider)
    problems = services.problems.list_problems()
    manifest = load_golden_manifest()
    proposed = await _draft_biggest_problem(services, provider, problems)
    report = {"E1": _problem_formation(services, problems, manifest),
              "E3": _groundedness(services, problems, proposed),
              "E7": _redaction(services, manifest)}
    report["hard_gates_pass"] = (report["E3"]["uncited_claims"] == 0 and report["E7"]["secrets_in_store"] == 0
                                 and not report["E7"]["seeded_markers_leaked"])
    return report


async def _ingest(services: Any, provider: Any) -> None:
    """W1 + W2 over the synthetic golden connectors."""
    ingestion = build_ingestion_app(services, provider)
    sessions = InMemorySessionService()
    runner = Runner(app=ingestion, session_service=sessions)
    session = await sessions.create_session(app_name=ingestion.name, user_id="eval")
    await _run(runner, "eval", session.id, types.Content(role="user", parts=[types.Part(text="ingest")]))


def _problem_formation(services: Any, problems: list[Any], manifest: dict[str, Any]) -> dict[str, Any]:
    """E1: clustering quality against the golden manifest and the AC1 multi-source criterion."""
    signals_of = {p.problem_id: [s for s in (services.evidence.get_signal(link.signal_id) for link in p.linked_signals)
                                 if s is not None] for p in problems}
    systems_per_problem = [len({s.source_system for s in signals}) for signals in signals_of.values()]
    clustering = _pairwise(manifest, {(s.source_system.value, s.source_record_id): pid
                                      for pid, signals in signals_of.items() for s in signals})
    return {"problems": len(problems), "expected_problems": len(manifest["problems"]) + 1, **clustering,
            "max_source_systems_in_one_problem": max(systems_per_problem, default=0),
            "ac1_multi_source_problem": max(systems_per_problem, default=0) >= 3}


def _groundedness(services: Any, problems: list[Any], proposed: dict[str, Any]) -> dict[str, Any]:
    """E3 (hard gate): no citation-required claim without a citation, anywhere."""
    uncited = sum(1 for p in problems for _, c in iter_claims(p) if c.kind in CITATION_REQUIRED and not c.citations)
    failing = sum(1 for p in problems
                  if services.verifier.verify_subject(p, subject_type="product_problem").overall.value != "PASS")
    return {"uncited_claims": uncited + proposed.get("uncited_claims", 0), "problems_failing_verification": failing,
            "requirement": proposed}


def _redaction(services: Any, manifest: dict[str, Any]) -> dict[str, Any]:
    """E7 (hard gate): nothing secret or raw-PII in the store, and no seeded marker survived."""
    leaks: dict[str, int] = {}
    stored = services.evidence.list_signals()
    for signal in stored:
        for info_type, count in services.redactor.inspect(signal.raw_text).items():
            leaks[info_type] = leaks.get(info_type, 0) + count
    stored_text = "\n".join(signal.raw_text for signal in stored)
    markers = [x["raw_marker"] for x in manifest.get("secrets", [])] + list(manifest.get("raw_pii_markers", []))
    secret_types = getattr(services.redactor, "secret_types", SECRET_TYPES)
    return {"secrets_in_store": sum(v for k, v in leaks.items() if k in secret_types),
            "raw_pii_in_store": sum(leaks.get(k, 0) for k in ("EMAIL_ADDRESS", "PHONE_NUMBER")),
            "seeded_markers_leaked": [m for m in markers if m in stored_text]}


def _pairwise(manifest: dict[str, Any], predicted: dict[tuple[str, str], str]) -> dict[str, Any]:
    """Pairwise precision/recall of predicted problems against the golden manifest (noise = its own cluster)."""
    expected = {(r["system"], r["record_id"]): pid for pid, problem in manifest["problems"].items()
                for r in problem["records"]}
    expected.update({(r["system"], r["record_id"]): "noise" for r in manifest.get("noise", [])})
    keys = sorted(set(expected) & set(predicted))
    tp = fp = fn = 0
    for i, a in enumerate(keys):
        for b in keys[i + 1:]:
            same_expected, same_predicted = expected[a] == expected[b], predicted[a] == predicted[b]
            tp += same_expected and same_predicted
            fp += same_predicted and not same_expected
            fn += same_expected and not same_predicted
    return {"pairwise_precision": round(tp / (tp + fp), 4) if tp + fp else 1.0,
            "pairwise_recall": round(tp / (tp + fn), 4) if tp + fn else 1.0,
            "records_scored": len(keys), "records_expected": len(expected)}


async def _draft_biggest_problem(services: Any, provider: Any, problems: list[Any]) -> dict[str, Any]:
    """Run W3 on the most-reported problem, answering questions with canned PM input, stopping at approval."""
    if not problems:
        return {"status": "no_problem"}
    target = max(problems, key=lambda p: len(p.linked_signals))
    core = build_core_app(services, provider)
    sessions = InMemorySessionService()
    runner = Runner(app=core, session_service=sessions)
    session = await sessions.create_session(app_name=core.name, user_id=EVAL_PM.sub)
    message = types.Content(role="user", parts=[types.Part(text=json.dumps(
        {"capability": "define_requirement", "problem_id": target.problem_id}))])
    events, invocation = await _run(runner, EVAL_PM.sub, session.id, message)
    for _ in range(4):
        pauses = pending_interrupts(events)
        if not pauses:
            break
        pause = pauses[0]
        if "action_id" in pause.payload:
            action = services.approvals.get_action(pause.payload["action_id"])
            _, draft = services.subjects.get(action.draft_ref)
            uncited = sum(1 for _, c in iter_claims(draft) if c.kind in CITATION_REQUIRED and not c.citations)
            return {"status": "proposed_awaiting_pm", "action_type": action.action_type.value,
                    "uncited_claims": uncited, "fields": {k: v.status.value for k, v in draft.fields.items()}}
        answers = [{"question_id": q["question_id"], "checklist_item": q["checklist_item"],
                    "text": CANNED_ANSWERS.get(q["checklist_item"], "No further input.")}
                   for q in pause.payload.get("questions", [])]
        reply = {"answers": answers, "assertion": services.identity.issue(EVAL_PM)}
        events, invocation = await _run(runner, EVAL_PM.sub, session.id, resume_content(pause.interrupt_id, reply),
                                        invocation)
    outputs = [e.output for e in events if e.output is not None]
    return {"status": (outputs[-1] or {}).get("status", "unknown") if outputs else "unknown"}


def main() -> int:
    report = asyncio.run(evaluate())
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if report["hard_gates_pass"] else 1


if __name__ == "__main__":
    sys.exit(main())
