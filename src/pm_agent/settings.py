"""Runtime settings for every PM Agent component (one typed model over ``config/pm_agent.yaml``).

Workspace rules enforced here (GEMINI.md):
* Gemini is reached only through the Gen AI SDK on Gemini Enterprise Agent Platform
  (``genai.Client(enterprise=True, project=..., location=...)``); API keys are refused.
* ``GOOGLE_CLOUD_PROJECT`` / ``GOOGLE_CLOUD_LOCATION`` override the file (local dev); on Agent
  Runtime the platform provides them.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml
from pydantic import BaseModel, ConfigDict, Field

from pm_agent.placeholders import PlaceholderNotResolved, find_placeholders

if TYPE_CHECKING:  # pragma: no cover
    from google import genai

DEFAULT_CONFIG_PATH = Path(__file__).resolve().parents[2] / "config" / "pm_agent.yaml"


class ConfigurationError(RuntimeError):
    """Invalid or forbidden configuration."""


class Environment(StrEnum):
    LOCAL = "local"
    DEV = "dev"
    STAGE = "stage"
    PROD = "prod"


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class GcpSettings(_Model):
    project_id: str
    location: str
    org_id: str
    project_number: str


class ModelSettings(_Model):
    router: str
    drafter: str
    judge: str
    embedding: str
    embedding_fallback: str
    allow_non_regional_endpoints: bool = False
    non_regional_location: str


class VerifierSettings(_Model):
    version: str
    judge_required: bool = True
    dual_judge_action_types: list[str] = Field(default_factory=list)
    max_quote_chars: int = 400
    near_duplicate_threshold: float = 0.88
    report_max_age_hours: int = 24


class CorrelationSettings(_Model):
    signal_minhash_jaccard: float = 0.85
    scoring: str = "lexical"              # lexical (offline/reference) | embedding (production, gemini-embedding)
    assign_threshold: float = 0.82        # embedding scorer
    adjudicate_floor: float = 0.65        # embedding scorer
    lexical_assign_threshold: float = 0.55
    lexical_adjudicate_floor: float = 0.35
    window_days: int = 90


class ApproverRule(_Model):
    groups: list[str]
    min_approvals: int = Field(default=1, ge=1)
    require_each_group: bool = False  # two-person rule "PM + UX": one distinct approver from every listed group


class HitlSettings(_Model):
    proposal_ttl_hours: int = 72
    token_ttl_minutes: int = 15
    token_issuer: str
    token_audience: str = "commit-executor"
    clock_skew_seconds: int = 30
    approver_rules: dict[str, ApproverRule]
    idp_group_mapping: dict[str, str]


class PolicySettings(_Model):
    prohibited_actions: list[str]
    outreach_allowed_domains: list[str]
    global_kb_forbids_customer_names: bool = True
    min_dls_reuse_ratio: float = 0.9


class ProductSettings(_Model):
    pilot_product_line: str
    product_aliases: dict[str, list[str]] = Field(default_factory=dict)


class GroundingSettings(_Model):
    gazetteers: dict[str, Any] = Field(default_factory=dict)
    atom_patterns: dict[str, str] = Field(default_factory=dict)
    custom_infotypes: dict[str, str] = Field(default_factory=dict)


class ReadinessChecklistSettings(_Model):
    id: str
    items: list[str]


class TemplateSettings(_Model):
    work_item_type: str
    fields: list[str]
    ado_field_map: dict[str, str] = Field(default_factory=dict)
    advisory_fields: list[str] = Field(default_factory=list)


class Settings(_Model):
    environment: Environment = Environment.LOCAL
    config_version: str
    gcp: GcpSettings
    models: ModelSettings
    verifier: VerifierSettings
    correlation: CorrelationSettings
    hitl: HitlSettings
    policies: PolicySettings
    product: ProductSettings
    grounding: GroundingSettings
    readiness_checklist: ReadinessChecklistSettings
    templates: dict[str, TemplateSettings]
    connectors: dict[str, dict[str, Any]]
    outreach: dict[str, Any] = Field(default_factory=dict)

    @property
    def strict(self) -> bool:
        """Stage and prod must not run with unresolved placeholders."""
        return self.environment in (Environment.STAGE, Environment.PROD)

    def unresolved_placeholders(self) -> list[tuple[str, str, str]]:
        """Every ``(path, question_id, name)`` still waiting for SLB input."""
        return find_placeholders(self.model_dump(mode="json"))


def load_settings(path: Path | None = None, environ: Mapping[str, str] | None = None) -> Settings:
    """Load, override from the environment, validate, and enforce placeholder strictness."""
    env = os.environ if environ is None else environ
    if env.get("GOOGLE_API_KEY") or env.get("GEMINI_API_KEY"):
        raise ConfigurationError(
            "GOOGLE_API_KEY/GEMINI_API_KEY are forbidden: use Gemini Enterprise Agent Platform via "
            "genai.Client(enterprise=True, ...) with Application Default Credentials (GEMINI.md).")
    config_path = path or Path(env.get("PM_AGENT_CONFIG", str(DEFAULT_CONFIG_PATH)))
    with config_path.open(encoding="utf-8") as handle:
        data: dict[str, Any] = yaml.safe_load(handle) or {}

    if env.get("PM_AGENT_ENV"):
        data["environment"] = env["PM_AGENT_ENV"]
    gcp = dict(data.get("gcp") or {})
    if env.get("GOOGLE_CLOUD_PROJECT"):
        gcp["project_id"] = env["GOOGLE_CLOUD_PROJECT"]
    if env.get("GOOGLE_CLOUD_LOCATION"):
        gcp["location"] = env["GOOGLE_CLOUD_LOCATION"]
    data["gcp"] = gcp

    settings = Settings.model_validate(data)
    if settings.strict:
        unresolved = settings.unresolved_placeholders()
        if unresolved:
            first_path, question_id, name = unresolved[0]
            listing = ", ".join(f"{qid}:{n} ({p})" for p, qid, n in unresolved)
            raise PlaceholderNotResolved(question_id, name, f"unresolved in {settings.environment} at {first_path}; "
                                                            f"all unresolved: {listing}")
    return settings


def genai_client_kwargs(settings: Settings) -> dict[str, Any]:
    """Arguments for ``google.genai.Client`` — always Gemini Enterprise Agent Platform, never API keys."""
    return {"enterprise": True, "project": settings.gcp.project_id, "location": settings.gcp.location}


def make_genai_client(settings: Settings) -> "genai.Client":
    """Build the Gen AI SDK client (Application Default Credentials; run `gcloud auth application-default login`)."""
    from google import genai  # pylint: disable=import-outside-toplevel

    return genai.Client(**genai_client_kwargs(settings))
