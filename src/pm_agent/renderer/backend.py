"""``svc-prototype-renderer`` backend: the write handler behind ``release_prototype_spec`` (ADR-006 Path A).

The executor calls the tool with the approved payload (spec id + subject hash). The backend re-loads the spec,
checks it is byte-for-byte the approved one, compiles the Plugin API program from verified claims only and stores it
for the private SLB Figma plugin to fetch. Program storage is in-memory here (production: a CMEK GCS bucket).
"""

from __future__ import annotations

from typing import Any

from pm_agent.connectors.recording import RecordingWriteBackend
from pm_agent.gateway import ToolCallError
from pm_agent.grounding.verifier import subject_digest
from pm_agent.hitl.subjects import InMemorySubjectStore
from pm_agent.mcp_servers.write_server import WriteHandler
from pm_agent.prototype.compile import compile_plugin_program
from pm_agent.schemas import PrototypeSpec, RequirementDraft, sha256_hex


class RendererBackend(RecordingWriteBackend):
    def __init__(self, subjects: InMemorySubjectStore, base_url: str = "https://renderer.example.invalid") -> None:
        super().__init__(system="svc-prototype-renderer")
        self._subjects = subjects
        self._base_url = base_url.rstrip("/")
        self.programs: dict[str, str] = {}

    def _load(self, payload: dict[str, Any]) -> tuple[PrototypeSpec, dict[str, str]]:
        spec_ref = f"prototype_spec:{payload.get('spec_id')}:v1"
        try:
            _, spec = self._subjects.get(spec_ref)
        except LookupError as exc:
            raise ToolCallError("not_found", "approved prototype spec not found") from exc
        if not isinstance(spec, PrototypeSpec) or subject_digest(spec) != payload.get("spec_sha256"):
            raise ToolCallError("validation_failed", "stored spec differs from the approved spec")
        claims: dict[str, str] = {}
        for _, subject in self._subjects.items.values():
            if isinstance(subject, RequirementDraft) and subject.requirement_id == spec.requirement_id:
                claims.update({c.claim_id: c.text for c in subject.all_claims()})
        return spec, claims

    def handler(self, tool: str) -> WriteHandler:
        recorded = super().handler(tool)

        def _handle(*, payload: Any, target: dict[str, Any], validate_only: bool,
                    idempotency_key: str) -> dict[str, Any]:
            spec, claims = self._load(payload)
            program = compile_plugin_program(spec, claims)  # compiling is the pre-flight check
            result = recorded(payload=payload, target=target, validate_only=validate_only,
                              idempotency_key=idempotency_key)
            if validate_only:
                return result
            program_id = "prg_" + sha256_hex(program)[:24]
            self.programs[program_id] = program
            return {**result, "external_id": program_id, "url": f"{self._base_url}/programs/{program_id}"}

        return _handle
