"""W1 — signal ingestion (``signal_ingestion`` Agent Runtime app, docs/05 §4.1).

``plan_window → poll_<source> ×N (parallel) → join_sources → dedup_signals → store_signals → problem_builder (W2)
→ emit_events``. Connectors return already-redacted ``Signal``s; a poll node only catches ``ConnectorError`` and
reports the source as unavailable (IR3 — partial data is loud, never silently empty).
"""

from __future__ import annotations

from typing import Any

from google.adk import Workflow
from google.adk.apps import App, ResumabilityConfig
from google.adk.workflow import FunctionNode, JoinNode

from pm_agent.agents.factory import LlmNodes, ModelProvider
from pm_agent.connectors.base import ConnectorError, ConnectorStatus, SourceConnector
from pm_agent.grounding.chunking import chunk_signal
from pm_agent.grounding.dedup import jaccard_minhash
from pm_agent.placeholders import PlaceholderNotImplemented
from pm_agent.runtime import Services
from pm_agent.schemas import Signal, SourceSystem
from pm_agent.settings import ConfigurationError
from pm_agent.workflows.problem_builder import build_problem_builder

APP_NAME = "signal_ingestion"


def _poller(source: SourceSystem, connector: SourceConnector) -> Any:
    def poll(node_input: dict[str, Any]) -> dict[str, Any]:
        cursor = (node_input.get("cursors") or {}).get(source.value)
        try:
            result = connector.poll(cursor)
        except ConnectorError as exc:
            return {"source": source.value, "status": exc.code, "reason": str(exc), "signals": [],
                    "next_cursor": cursor}
        except PlaceholderNotImplemented as exc:  # the source's SLB integration details are not supplied yet
            return {"source": source.value, "status": "not_configured", "reason": f"awaiting {exc.question_id}",
                    "signals": [], "next_cursor": cursor}
        return {"source": source.value, "status": result.status.value, "reason": result.reason,
                "signals": [s.model_dump(mode="json") for s in result.signals], "next_cursor": result.next_cursor}

    return FunctionNode(func=poll, name=f"poll_{source.value}")


def build_ingestion_workflow(services: Services, provider: ModelProvider) -> Workflow:
    """The W1 graph over every configured connector (fan-out + ``JoinNode``)."""
    llm = LlmNodes(provider)
    threshold = services.settings.correlation.signal_minhash_jaccard
    cursors: dict[str, str | None] = {}

    def plan_window(node_input: Any) -> dict[str, Any]:
        del node_input
        return {"cursors": dict(cursors), "sources": sorted(s.value for s in services.connectors)}

    def dedup_signals(node_input: dict[str, Any]) -> dict[str, Any]:
        """Exact duplicates (same provenance) are dropped; cross-system near-duplicates become twins."""
        health, unique, seen = {}, [], set()
        dropped = 0
        for output in node_input.values():
            health[output["source"]] = output["status"]
            if output["status"] == ConnectorStatus.OK.value:
                cursors[output["source"]] = output["next_cursor"]
            for data in output["signals"]:
                signal = Signal.model_validate(data)
                key = signal.provenance_hash
                if key in seen or services.evidence.get_signal(signal.signal_id) is not None:
                    dropped += 1
                    continue
                seen.add(key)
                unique.append(signal)
        twins: dict[str, str] = {}
        ordered = sorted(unique, key=lambda s: (s.captured_utc, s.signal_id))
        for index, signal in enumerate(ordered):
            for earlier in ordered[:index]:
                if earlier.source_system is not signal.source_system and earlier.signal_id not in twins \
                        and jaccard_minhash(signal.raw_text, earlier.raw_text) >= threshold:
                    twins[signal.signal_id] = earlier.signal_id
                    break
        return {"signals": [s.model_dump(mode="json") for s in ordered], "twins": twins, "dropped": dropped,
                "source_health": health}

    def store_signals(node_input: dict[str, Any]) -> dict[str, Any]:
        stored = []
        for data in node_input["signals"]:
            signal = Signal.model_validate(data)
            services.evidence.add_signal(signal)
            for chunk in chunk_signal(signal):
                services.evidence.add_chunk(chunk)
            stored.append(signal.signal_id)
        return {"signal_ids": stored, "twins": node_input["twins"],
                "summary": {"stored": len(stored), "dropped": node_input["dropped"],
                            "source_health": node_input["source_health"]}}

    def emit_events(node_input: dict[str, Any]) -> dict[str, Any]:
        """Summary for operators (production: Pub/Sub ``signal-ingested`` + adjudication queue)."""
        return {**(node_input.get("summary") or {}), "problems": node_input.get("problems", []),
                "needs_confirmation": node_input.get("needs_confirmation", [])}

    polls = tuple(_poller(source, connector) for source, connector in sorted(services.connectors.items()))
    if not polls:
        raise ConfigurationError("signal_ingestion needs at least one source connector")
    return Workflow(name=APP_NAME, edges=[("START", plan_window, polls, JoinNode(name="join_sources"), dedup_signals,
                                           store_signals, build_problem_builder(services, llm), emit_events)])


def build_ingestion_app(services: Services, provider: ModelProvider) -> App:
    return App(name=APP_NAME, root_agent=build_ingestion_workflow(services, provider),
               resumability_config=ResumabilityConfig(is_resumable=True))
