"""The closed, deny-by-default action catalog (docs/07 §2).

Each state-changing action maps to exactly one write tool on one write server, the subject types that may back it,
and the approver rule from configuration. Anything not in this catalog cannot be proposed, approved or executed.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from pm_agent.grounding.policy import ACTION_SUBJECT_TYPES
from pm_agent.schemas import ActionType
from pm_agent.settings import ConfigurationError, Settings


class UnknownAction(LookupError):
    """The action type is not in the catalog (P-01 deny by default)."""


@dataclass(frozen=True)
class _Binding:
    server: str
    tool: str
    target_system: str
    preflight: bool


# MCP tool names are bare verb_object because Agent Gateway CEL matches ``mcp.toolName`` per server (docs/09 §1).
_BINDINGS: dict[ActionType, _Binding] = {
    ActionType.ADO_CREATE_WORK_ITEM: _Binding("mcp-ado-write", "create_work_item", "ado", True),
    ActionType.ADO_ADD_LINKS: _Binding("mcp-ado-write", "add_relations", "ado", True),
    ActionType.KB_PUBLISH_ARTICLE: _Binding("mcp-kb-write", "publish_article", "kb", True),
    ActionType.FIGMA_RENDER_PROTOTYPE: _Binding("svc-prototype-renderer", "release_prototype_spec", "figma", True),
    ActionType.FIGMA_POST_COMMENT: _Binding("mcp-figma-write", "post_comment", "figma", True),
    ActionType.PROTOTYPE_RELEASE_INTERNAL: _Binding("mcp-ledger", "release_validation_package", "internal", False),
    ActionType.OUTREACH_SEND_INTERNAL: _Binding("mcp-msgraph-write", "send_mail", "msgraph", True),
    ActionType.ROADMAP_TRACE_LINK: _Binding("mcp-ledger", "record_trace_links", "internal", False),
}


def scope_for(server: str, tool: str) -> str:
    """Approval-token scope for a tool: ``mcp-ado-write`` + ``create_work_item`` → ``ado_write.create_work_item``."""
    return f"{server.removeprefix('mcp-').replace('-', '_')}.{tool}"


@dataclass(frozen=True)
class ActionSpec:
    action_type: ActionType
    server: str
    tool: str
    target_system: str
    preflight: bool
    subject_types: frozenset[str]
    approver_groups: tuple[str, ...]
    min_approvals: int
    require_each_group: bool

    @property
    def scope(self) -> str:
        return scope_for(self.server, self.tool)


class ActionCatalog:
    """Action type → write binding + approver rule. Built once from configuration at start-up."""

    def __init__(self, specs: Iterable[ActionSpec]) -> None:
        self._specs = {spec.action_type.value: spec for spec in specs}

    @classmethod
    def from_settings(cls, settings: Settings) -> "ActionCatalog":
        """Combine the static write bindings with ``hitl.approver_rules``; every action must have a rule."""
        specs = []
        for action_type, binding in _BINDINGS.items():
            rule = settings.hitl.approver_rules.get(action_type.value)
            if rule is None:
                raise ConfigurationError(f"hitl.approver_rules has no rule for {action_type.value}")
            if rule.require_each_group and rule.min_approvals < len(rule.groups):
                raise ConfigurationError(f"{action_type.value}: require_each_group needs min_approvals >= groups")
            specs.append(ActionSpec(action_type=action_type, server=binding.server, tool=binding.tool,
                                    target_system=binding.target_system, preflight=binding.preflight,
                                    subject_types=ACTION_SUBJECT_TYPES[action_type.value],
                                    approver_groups=tuple(rule.groups), min_approvals=rule.min_approvals,
                                    require_each_group=rule.require_each_group))
        return cls(specs)

    def get(self, action_type: str) -> ActionSpec:
        try:
            return self._specs[action_type]
        except KeyError:
            raise UnknownAction(f"{action_type} is not in the action catalog") from None

    def specs(self) -> list[ActionSpec]:
        return list(self._specs.values())

    def write_tools(self) -> frozenset[str]:
        """Bare names of every write tool — never allow-listed for an LLM agent (GroundingGuard)."""
        return frozenset(spec.tool for spec in self._specs.values())

    def write_servers(self) -> frozenset[str]:
        return frozenset(spec.server for spec in self._specs.values())
