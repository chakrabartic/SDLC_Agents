"""``commit_executor`` Agent Runtime app — the zero-LLM write path (docs/05 §4.7, docs/07 §7).

There is no ``Agent`` in this app, so there is no model to prompt-inject. The commit steps
(load → consume token → verify token → re-verify → pre-flight → execute → read-back → record) run inside **one**
function node on purpose: the approval token lives only in that node's memory and never enters session state, events
or logs. Node-level retries are disabled — a consumed token must not be replayed; vendor retries happen inside the
connector call.
"""

from __future__ import annotations

import re
from typing import Any

from google.adk import Workflow
from google.adk.apps import App, ResumabilityConfig
from google.adk.workflow import FunctionNode

from pm_agent.hitl.executor import CommitExecutor
from pm_agent.workflows.common import request_text

APP_NAME = "commit_executor"
ACTION_ID = re.compile(r"\bact_[0-9A-Z]{26}\b")


def build_executor_workflow(executor: CommitExecutor, executor_id: str) -> Workflow:
    """``START → parse_request → execute_commit`` (function nodes only)."""
    def parse_request(node_input: Any) -> dict[str, Any]:
        """Accept ``{"action_id": ...}`` or text containing one ``act_…`` id; anything else is refused."""
        if isinstance(node_input, dict) and isinstance(node_input.get("action_id"), str):
            candidate = node_input["action_id"]
        else:
            match = ACTION_ID.search(request_text(node_input))
            candidate = match.group(0) if match else ""
        return {"action_id": candidate if ACTION_ID.fullmatch(candidate or "") else None}

    def execute_commit(node_input: dict[str, Any]) -> dict[str, Any]:
        action_id = node_input.get("action_id")
        if not action_id:
            return {"action_id": None, "outcome": "refused", "error": "no action id"}
        result = executor.execute(action_id, executor_id=executor_id)
        return {"action_id": result.action_id, "outcome": result.outcome, "external_ref": result.external_ref,
                "error": result.error}

    return Workflow(name=APP_NAME, edges=[("START", parse_request,
                                           FunctionNode(func=execute_commit, name="execute_commit"))])


def build_executor_app(executor: CommitExecutor, executor_id: str) -> App:
    return App(name=APP_NAME, root_agent=build_executor_workflow(executor, executor_id),
               resumability_config=ResumabilityConfig(is_resumable=True))
