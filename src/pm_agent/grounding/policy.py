"""Policy-as-code for proposed actions (docs/06 §7) — deterministic, deny by default."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from email.utils import getaddresses
from typing import Any

from pm_agent.schemas import ActionType
from pm_agent.settings import Settings

_DOMAIN = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?)+")
POLICY_VERSION = "policy-1"


def _recipient_domain(recipient: str) -> str | None:
    """Extract the RFC 5322 domain of a single recipient, rejecting CR/LF, multi-address, double-@ and %-hack forms."""
    if "\r" in recipient or "\n" in recipient:
        return None
    parsed = getaddresses([recipient])
    if len(parsed) != 1:
        return None
    address = parsed[0][1].strip()
    if address.count("@") != 1:
        return None
    local, _, domain = address.partition("@")
    if not local or "%" in local or not _DOMAIN.fullmatch(domain):
        return None
    return domain.lower()

# Which verified subject may back which write (assess-before-mutation: an approval for a KB article can never be
# replayed as an ADO work item). Consumed by V9 and the HITL action catalog.
ACTION_SUBJECT_TYPES: dict[str, frozenset[str]] = {
    ActionType.ADO_CREATE_WORK_ITEM.value: frozenset({"requirement_draft"}),
    ActionType.ADO_ADD_LINKS.value: frozenset({"requirement_draft", "trace_link"}),
    ActionType.KB_PUBLISH_ARTICLE.value: frozenset({"knowledge_article_draft"}),
    ActionType.FIGMA_RENDER_PROTOTYPE.value: frozenset({"prototype_spec"}),
    ActionType.FIGMA_POST_COMMENT.value: frozenset({"prototype_spec"}),
    ActionType.PROTOTYPE_RELEASE_INTERNAL.value: frozenset({"prototype_spec"}),
    ActionType.OUTREACH_SEND_INTERNAL.value: frozenset({"product_problem", "investigation_pack"}),
    ActionType.ROADMAP_TRACE_LINK.value: frozenset({"trace_link"}),
}


@dataclass
class PolicyDecision:
    allowed: bool
    rule_ids: list[str] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)

    def deny(self, rule_id: str, reason: str) -> None:
        self.allowed = False
        self.rule_ids.append(rule_id)
        self.reasons.append(reason)


class PolicyEngine:
    """Evaluates P-01…P-07 for an action payload (P-08 freshness is enforced by the ledger)."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        orgs = settings.grounding.gazetteers.get("organizations") or {}
        self._customer_terms = sorted({t.lower() for canon, forms in orgs.items() for t in [canon, *forms]},
                                      key=len, reverse=True)

    def evaluate(self, action_type: str, payload: dict[str, Any], target: dict[str, Any]) -> PolicyDecision:
        """Deny-by-default evaluation of one proposed action; every denial carries its rule id (P-xx)."""
        decision = PolicyDecision(allowed=True)
        policies = self._settings.policies
        if action_type in policies.prohibited_actions:
            decision.deny("P-02", f"{action_type} is a prohibited action (GOV5)")
            return decision
        if action_type not in {a.value for a in ActionType}:
            decision.deny("P-01", f"{action_type} is not in the action catalog (deny by default)")
            return decision
        if action_type == ActionType.OUTREACH_SEND_INTERNAL:
            self._outreach(payload, decision)
        if action_type == ActionType.KB_PUBLISH_ARTICLE:
            self._kb_audience(payload, decision)
        if action_type == ActionType.FIGMA_RENDER_PROTOTYPE:
            self._dls_reuse(payload, decision)
        del target  # reserved for target-scoped rules (area paths, KB spaces) once Q-01/Q-04 are resolved
        return decision

    def _outreach(self, payload: dict[str, Any], decision: PolicyDecision) -> None:
        allowed = {d.lower() for d in self._settings.policies.outreach_allowed_domains}
        recipients = [str(r) for r in payload.get("to", [])] + [str(r) for r in payload.get("cc", [])]
        if not recipients:
            decision.deny("P-03", "outreach without recipients")
        for recipient in recipients:
            domain = _recipient_domain(recipient)
            if domain is None or not any(domain == d or domain.endswith("." + d) for d in allowed):
                decision.deny("P-03", f"recipient domain '{domain or recipient}' is external (GOV5)")

    def _kb_audience(self, payload: dict[str, Any], decision: PolicyDecision) -> None:
        if payload.get("audience") != "global" or not self._settings.policies.global_kb_forbids_customer_names:
            return
        if payload.get("customer_cleared"):
            return
        text = json.dumps(payload, ensure_ascii=False).lower()
        for term in self._customer_terms:
            if re.search(rf"(?<![a-z0-9]){re.escape(term)}(?![a-z0-9])", text):
                decision.deny("P-04", f"global KB article names a customer ('{term}') without customer_cleared")
                return

    def _dls_reuse(self, payload: dict[str, Any], decision: PolicyDecision) -> None:
        ratio = float(payload.get("reuse_ratio", 0.0))
        if ratio < self._settings.policies.min_dls_reuse_ratio:
            decision.deny("P-07", f"DLS reuse ratio {ratio:.2f} below {self._settings.policies.min_dls_reuse_ratio}")
        for pattern in payload.get("new_patterns", []):
            if not pattern.get("justification"):
                decision.deny("P-07", f"new pattern '{pattern.get('name')}' has no justification (FR-B3)")
