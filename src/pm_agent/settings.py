"""Runtime settings for every PM Agent component (one typed model over ``config/pm_agent.yaml``).

Workspace rules enforced here (GEMINI.md):
* Gemini is reached only through the Gen AI SDK on Gemini Enterprise Agent Platform
  (``genai.Client(enterprise=True, project=..., location=...)``); API keys are refused.
* ``GOOGLE_CLOUD_PROJECT`` / ``GOOGLE_CLOUD_LOCATION`` override the file (local dev); on Agent
  Runtime the platform provides them.

Invariants (docs/06, docs/12 §4): thresholds are range-checked and closed choices are typed, so a typo fails
validation instead of silently falling back (an unknown dual-judge action type used to disable dual judging). Stage
and prod additionally refuse unresolved placeholders anywhere in the config and fail-open configuration: a disabled
entailment judge (V5) or missing gazetteers (P-04 and V4 aliasing read them with ``.get(...) or {}``).
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from pm_agent.placeholders import PlaceholderNotResolved, find_placeholders
from pm_agent.schemas.claims import MAX_QUOTE_CHARS
from pm_agent.schemas.hitl import ActionType

if TYPE_CHECKING:  # pragma: no cover
    from google import genai

DEFAULT_CONFIG_PATH = Path(__file__).resolve().parents[2] / "config" / "pm_agent.yaml"
# Gazetteer keys read by grounding.atoms (aliases), grounding.policy (P-04) and correlation.scoring (identities).
REQUIRED_GAZETTEERS = ("organizations", "products")

Ratio = Annotated[float, Field(ge=0.0, le=1.0)]
Positive = Annotated[int, Field(ge=1)]


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
    max_quote_chars: int = Field(default=MAX_QUOTE_CHARS, ge=1, le=MAX_QUOTE_CHARS)  # never above Citation.quote
    near_duplicate_threshold: Ratio = 0.88
    report_max_age_hours: Positive = 24

    @field_validator("dual_judge_action_types")
    @classmethod
    def _known_action_types(cls, value: list[str]) -> list[str]:
        unknown = sorted(set(value) - {action.value for action in ActionType})
        if unknown:
            raise ValueError(f"unknown action types {unknown}: a typo would silently disable dual judging")
        return value


class CorrelationSettings(_Model):
    signal_minhash_jaccard: Ratio = 0.85
    scoring: Literal["lexical", "embedding"] = "lexical"  # lexical (offline/reference) | embedding (gemini-embedding)
    assign_threshold: Ratio = 0.82        # embedding scorer
    adjudicate_floor: Ratio = 0.65        # embedding scorer
    lexical_assign_threshold: Ratio = 0.55
    lexical_adjudicate_floor: Ratio = 0.35
    window_days: Positive = 90

    @model_validator(mode="after")
    def _grey_zone_ordered(self) -> "CorrelationSettings":
        """Each scorer's adjudication floor sits at or below its assign threshold (docs/06 §6.2)."""
        pairs = ((self.adjudicate_floor, self.assign_threshold),
                 (self.lexical_adjudicate_floor, self.lexical_assign_threshold))
        if any(floor > assign for floor, assign in pairs):
            raise ValueError("an adjudicate_floor may not exceed its assign_threshold")
        return self


class ApproverRule(_Model):
    groups: list[str]
    min_approvals: int = Field(default=1, ge=1)
    require_each_group: bool = False  # two-person rule "PM + UX": one distinct approver from every listed group


class HitlSettings(_Model):
    proposal_ttl_hours: Positive = 72
    token_ttl_minutes: Positive = 15
    token_issuer: str
    token_audience: str = "commit-executor"
    clock_skew_seconds: int = Field(default=30, ge=0)
    approver_rules: dict[str, ApproverRule]
    idp_group_mapping: dict[str, str]


class PolicySettings(_Model):
    prohibited_actions: list[str]
    outreach_allowed_domains: list[str]
    global_kb_forbids_customer_names: bool = True
    min_dls_reuse_ratio: Ratio = 0.9


class ProductSettings(_Model):
    pilot_product_line: str
    product_aliases: dict[str, list[str]] = Field(default_factory=dict)


def _is_str_list(value: Any) -> bool:
    return isinstance(value, list) and all(isinstance(item, str) for item in value)


class GroundingSettings(_Model):
    gazetteers: dict[str, Any] = Field(default_factory=dict)
    atom_patterns: dict[str, str] = Field(default_factory=dict)
    custom_infotypes: dict[str, str] = Field(default_factory=dict)

    @field_validator("gazetteers")
    @classmethod
    def _gazetteer_shapes(cls, value: dict[str, Any]) -> dict[str, Any]:
        """``organizations``: canonical name → aliases; ``products``: names. A wrong shape would read as empty."""
        organizations = value.get("organizations")
        if organizations is not None and not (isinstance(organizations, dict) and all(
                isinstance(name, str) and _is_str_list(aliases) for name, aliases in organizations.items())):
            raise ValueError("gazetteers.organizations must map each canonical name to a list of aliases")
        products = value.get("products")
        if products is not None and not _is_str_list(products):
            raise ValueError("gazetteers.products must be a list of names")
        return value


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
        """Every ``(path, question_id, name)`` still waiting for SLB input (embedded tokens and keys included)."""
        return find_placeholders(self.model_dump(mode="json"))


def strict_violations(settings: Settings) -> list[str]:
    """Fail-open configuration that is tolerable on a laptop but never in stage/prod (placeholders aside)."""
    problems = []
    if not settings.verifier.judge_required:
        problems.append("verifier.judge_required is false, so V5 entailment would never run")
    missing = [key for key in REQUIRED_GAZETTEERS if not settings.grounding.gazetteers.get(key)]
    if missing:
        problems.append(f"grounding.gazetteers lacks {missing} (P-04 and V4 aliasing would silently see none)")
    return problems


def _refuse_api_keys(env: Mapping[str, str]) -> None:
    if env.get("GOOGLE_API_KEY") or env.get("GEMINI_API_KEY"):
        raise ConfigurationError(
            "GOOGLE_API_KEY/GEMINI_API_KEY are forbidden: use Gemini Enterprise Agent Platform via "
            "genai.Client(enterprise=True, ...) with Application Default Credentials (GEMINI.md).")


def _apply_env_overrides(data: dict[str, Any], env: Mapping[str, str]) -> dict[str, Any]:
    """``PM_AGENT_ENV``, ``GOOGLE_CLOUD_PROJECT`` and ``GOOGLE_CLOUD_LOCATION`` win over the file."""
    merged = dict(data)
    if env.get("PM_AGENT_ENV"):
        merged["environment"] = env["PM_AGENT_ENV"]
    gcp = dict(merged.get("gcp") or {})
    if env.get("GOOGLE_CLOUD_PROJECT"):
        gcp["project_id"] = env["GOOGLE_CLOUD_PROJECT"]
    if env.get("GOOGLE_CLOUD_LOCATION"):
        gcp["location"] = env["GOOGLE_CLOUD_LOCATION"]
    merged["gcp"] = gcp
    return merged


def _enforce_strict(settings: Settings) -> None:
    """Stage/prod: no placeholder anywhere (values *or* keys, bare or embedded) and no fail-open configuration."""
    unresolved = settings.unresolved_placeholders()
    if unresolved:
        first_path, question_id, name = unresolved[0]
        listing = ", ".join(f"{qid}:{n} ({p})" for p, qid, n in unresolved)
        raise PlaceholderNotResolved(question_id, name, f"unresolved in {settings.environment} at {first_path}; "
                                                        f"all unresolved: {listing}")
    problems = strict_violations(settings)
    if problems:
        raise ConfigurationError(f"{settings.environment} refuses fail-open configuration: {'; '.join(problems)}")


def load_settings(path: Path | None = None, environ: Mapping[str, str] | None = None) -> Settings:
    """Load, override from the environment, validate, and enforce stage/prod strictness."""
    env = os.environ if environ is None else environ
    _refuse_api_keys(env)
    config_path = path or Path(env.get("PM_AGENT_CONFIG", str(DEFAULT_CONFIG_PATH)))
    with config_path.open(encoding="utf-8") as handle:
        data: dict[str, Any] = yaml.safe_load(handle) or {}
    settings = Settings.model_validate(_apply_env_overrides(data, env))
    if settings.strict:
        _enforce_strict(settings)
    return settings


def genai_client_kwargs(settings: Settings) -> dict[str, Any]:
    """Arguments for ``google.genai.Client`` — always Gemini Enterprise Agent Platform, never API keys."""
    return {"enterprise": True, "project": settings.gcp.project_id, "location": settings.gcp.location}


def make_genai_client(settings: Settings) -> "genai.Client":
    """Build the Gen AI SDK client (Application Default Credentials; run `gcloud auth application-default login`)."""
    from google import genai  # pylint: disable=import-outside-toplevel

    return genai.Client(**genai_client_kwargs(settings))
