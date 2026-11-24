"""signal-event-gateway — the public webhook ingress (GKE) for ADO, ServiceNow, PagerDuty, Microsoft Graph and Figma.

docs/09 §9 and docs/08 §8 (T8 webhook spoofing / flooding). Hints-only design: every delivery is verified (basic auth
or shared-secret header, HMAC-SHA256 over the raw body, Graph ``clientState``, Figma passcode), size-capped, and turned
into a ``ChangeHint`` that carries identifiers only — never body text. The ingestion workflow always re-reads the
record through the read MCP server, so even a spoofed hint can at worst cause a redundant re-read; nothing a webhook
says is ever trusted as data. Hints go to Pub/Sub ``signal-change-hints`` (``PubSubHintPublisher``).

Secrets come from Secret Manager via ``PM_AGENT_WEBHOOK_*`` environment variables (``WebhookSecrets.from_env``);
unconfigured (placeholder) secrets fail closed.
"""

from pm_agent.event_gateway.app import MAX_BODY_BYTES, create_app
from pm_agent.event_gateway.hints import ChangeHint, HintPublisher, InMemoryHintPublisher, PubSubHintPublisher
from pm_agent.event_gateway.secrets import WebhookSecrets

__all__ = [
    "MAX_BODY_BYTES",
    "ChangeHint",
    "HintPublisher",
    "InMemoryHintPublisher",
    "PubSubHintPublisher",
    "WebhookSecrets",
    "create_app",
]
