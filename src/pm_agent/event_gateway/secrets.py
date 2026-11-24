"""Webhook verification secrets for the signal-event-gateway (Secret Manager → environment → ``WebhookSecrets``).

Every default is a tracked placeholder (``PLACEHOLDER:Q-xx:…``). A secret that is empty or still a placeholder is
*not configured*, and verification against it always fails closed: placeholder tokens are committed to the repository,
so they are public and must never work as a credential (``verify.py`` never HMACs or compares with them).

Environment variables (values projected from Secret Manager; surrounding whitespace is stripped) and the placeholder
each one defaults to (listed literally so ``make placeholders`` registers them; a unit test keeps this list in sync):

* ``PM_AGENT_WEBHOOK_ADO_BASIC_USERNAME`` — ADO service-hook basic-auth user;
  ``PLACEHOLDER:Q-04:ado_webhook_basic_username``
* ``PM_AGENT_WEBHOOK_ADO_BASIC_PASSWORD`` — ADO service-hook basic-auth password;
  ``PLACEHOLDER:Q-04:ado_webhook_basic_password``
* ``PM_AGENT_WEBHOOK_ADO_SHARED_SECRET`` — value of the ADO custom header ``X-PM-Agent-Webhook-Secret``;
  ``PLACEHOLDER:Q-04:ado_webhook_shared_secret``
* ``PM_AGENT_WEBHOOK_SERVICENOW_HMAC_SECRET`` — ServiceNow outbound REST HMAC-SHA256 key;
  ``PLACEHOLDER:Q-05:servicenow_webhook_hmac_secret``
* ``PM_AGENT_WEBHOOK_PAGERDUTY_SIGNING_SECRETS`` — comma-separated PagerDuty v3 signing secrets (several may be active
  at once during rotation); ``PLACEHOLDER:Q-07:pagerduty_webhook_signing_secret``
* ``PM_AGENT_WEBHOOK_MSGRAPH_CLIENT_STATE`` — Graph subscription ``clientState``;
  ``PLACEHOLDER:Q-13:msgraph_subscription_client_state``
* ``PM_AGENT_WEBHOOK_FIGMA_PASSCODE`` — Figma webhook v2 ``passcode``; ``PLACEHOLDER:Q-02:figma_webhook_passcode``
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field

from pm_agent.placeholders import PREFIX, is_placeholder, placeholder

ENV_ADO_BASIC_USERNAME = "PM_AGENT_WEBHOOK_ADO_BASIC_USERNAME"
ENV_ADO_BASIC_PASSWORD = "PM_AGENT_WEBHOOK_ADO_BASIC_PASSWORD"
ENV_ADO_SHARED_SECRET = "PM_AGENT_WEBHOOK_ADO_SHARED_SECRET"
ENV_SERVICENOW_HMAC_SECRET = "PM_AGENT_WEBHOOK_SERVICENOW_HMAC_SECRET"
ENV_PAGERDUTY_SIGNING_SECRETS = "PM_AGENT_WEBHOOK_PAGERDUTY_SIGNING_SECRETS"
ENV_MSGRAPH_CLIENT_STATE = "PM_AGENT_WEBHOOK_MSGRAPH_CLIENT_STATE"
ENV_FIGMA_PASSCODE = "PM_AGENT_WEBHOOK_FIGMA_PASSCODE"


def is_configured(secret: str) -> bool:
    """A usable secret: non-empty and not a placeholder (placeholders are public, so they never verify anything)."""
    return bool(secret) and not is_placeholder(secret) and not secret.startswith(PREFIX)


@dataclass(frozen=True)
class WebhookSecrets:
    """Per-vendor webhook secrets. ``repr`` never shows values — only which vendors are still unconfigured."""

    ado_basic_username: str = field(default=placeholder("Q-04", "ado_webhook_basic_username"), repr=False)
    ado_basic_password: str = field(default=placeholder("Q-04", "ado_webhook_basic_password"), repr=False)
    ado_shared_secret: str = field(default=placeholder("Q-04", "ado_webhook_shared_secret"), repr=False)
    servicenow_hmac_secret: str = field(default=placeholder("Q-05", "servicenow_webhook_hmac_secret"), repr=False)
    pagerduty_signing_secrets: tuple[str, ...] = field(
        default=(placeholder("Q-07", "pagerduty_webhook_signing_secret"),), repr=False)
    msgraph_client_state: str = field(default=placeholder("Q-13", "msgraph_subscription_client_state"), repr=False)
    figma_passcode: str = field(default=placeholder("Q-02", "figma_webhook_passcode"), repr=False)

    def __repr__(self) -> str:
        return f"WebhookSecrets(<redacted>, unconfigured={','.join(self.unconfigured_vendors()) or 'none'})"

    def unconfigured_vendors(self) -> tuple[str, ...]:
        """Vendors whose webhooks can never verify with these secrets (every request from them is rejected)."""
        ado_basic = is_configured(self.ado_basic_username) and is_configured(self.ado_basic_password)
        ready = {
            "ado": ado_basic or is_configured(self.ado_shared_secret),
            "servicenow": is_configured(self.servicenow_hmac_secret),
            "pagerduty": any(is_configured(secret) for secret in self.pagerduty_signing_secrets),
            "msgraph": is_configured(self.msgraph_client_state),
            "figma": is_configured(self.figma_passcode),
        }
        return tuple(vendor for vendor, configured in ready.items() if not configured)

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> WebhookSecrets:
        """Read the ``PM_AGENT_WEBHOOK_*`` variables (Secret Manager → env); missing or blank → placeholder default.

        ``PM_AGENT_WEBHOOK_PAGERDUTY_SIGNING_SECRETS`` is comma-separated so old and new secrets can overlap during
        rotation. Values are stripped (Secret Manager payloads often end with a newline).
        """
        env = os.environ if environ is None else environ
        defaults = cls()

        def pick(name: str, default: str) -> str:
            return env.get(name, "").strip() or default

        rotation = tuple(part.strip() for part in env.get(ENV_PAGERDUTY_SIGNING_SECRETS, "").split(",") if part.strip())
        return cls(
            ado_basic_username=pick(ENV_ADO_BASIC_USERNAME, defaults.ado_basic_username),
            ado_basic_password=pick(ENV_ADO_BASIC_PASSWORD, defaults.ado_basic_password),
            ado_shared_secret=pick(ENV_ADO_SHARED_SECRET, defaults.ado_shared_secret),
            servicenow_hmac_secret=pick(ENV_SERVICENOW_HMAC_SECRET, defaults.servicenow_hmac_secret),
            pagerduty_signing_secrets=rotation or defaults.pagerduty_signing_secrets,
            msgraph_client_state=pick(ENV_MSGRAPH_CLIENT_STATE, defaults.msgraph_client_state),
            figma_passcode=pick(ENV_FIGMA_PASSCODE, defaults.figma_passcode),
        )
