"""Tests for settings loading, environment overrides, strict placeholder handling and the Gen AI client contract."""
from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

from pm_agent.placeholders import PlaceholderNotResolved
from pm_agent.schemas.claims import MAX_QUOTE_CHARS
from pm_agent.settings import (
    REQUIRED_GAZETTEERS,
    ConfigurationError,
    Environment,
    VerifierSettings,
    genai_client_kwargs,
    load_settings,
)

REPO_CONFIG = Path(__file__).resolve().parents[2] / "config" / "pm_agent.yaml"


def _resolve(value: Any) -> Any:
    """Replace every placeholder (stage/prod would refuse them) so the remaining strict checks can be exercised."""
    if isinstance(value, dict):
        return {key: _resolve(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_resolve(item) for item in value]
    return "resolved-by-test" if isinstance(value, str) and "PLACEHOLDER:" in value else value


def _config(tmp_path: Path, *, resolved: bool = False, **sections: dict[str, Any]) -> Path:
    """A copy of the repo config with per-section overrides (``None`` deletes a key)."""
    data = yaml.safe_load(REPO_CONFIG.read_text(encoding="utf-8"))
    data = _resolve(data) if resolved else data
    for section, changes in sections.items():
        merged = {**data.get(section, {}), **changes}
        data[section] = {key: value for key, value in merged.items() if value is not None}
    path = tmp_path / "pm_agent.yaml"
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    return path


def test_repo_config_loads_in_local_environment() -> None:
    settings = load_settings(REPO_CONFIG, environ={"PM_AGENT_ENV": "local"})
    assert settings.environment is Environment.LOCAL
    assert settings.gcp.location == "us-central1"
    assert settings.models.drafter == "gemini-3.8-flash"
    assert settings.hitl.token_ttl_minutes == 15
    assert not settings.strict


def test_environment_variables_override_project_and_location() -> None:
    settings = load_settings(REPO_CONFIG, environ={
        "PM_AGENT_ENV": "dev", "GOOGLE_CLOUD_PROJECT": "other-proj", "GOOGLE_CLOUD_LOCATION": "europe-west4"})
    assert settings.environment is Environment.DEV
    assert settings.gcp.project_id == "other-proj"
    assert settings.gcp.location == "europe-west4"


def test_unresolved_placeholders_are_listed() -> None:
    settings = load_settings(REPO_CONFIG, environ={"PM_AGENT_ENV": "local"})
    question_ids = {qid for _, qid, _ in settings.unresolved_placeholders()}
    assert {"Q-01", "Q-04", "Q-10"} <= question_ids


def test_strict_environment_rejects_placeholders() -> None:
    with pytest.raises(PlaceholderNotResolved):
        load_settings(REPO_CONFIG, environ={"PM_AGENT_ENV": "prod"})


def test_api_keys_are_forbidden() -> None:
    with pytest.raises(ConfigurationError, match="GOOGLE_API_KEY"):
        load_settings(REPO_CONFIG, environ={"PM_AGENT_ENV": "local", "GOOGLE_API_KEY": "x"})


def test_genai_client_uses_enterprise_agent_platform() -> None:
    settings = load_settings(REPO_CONFIG, environ={"PM_AGENT_ENV": "local"})
    assert genai_client_kwargs(settings) == {
        "enterprise": True, "project": "chayan-genai-bb", "location": "us-central1"}


# ------------------------------------------------------------------------------------- invariants (docs/06, docs/12)
def test_a_fully_resolved_config_loads_in_prod(tmp_path: Path) -> None:
    settings = load_settings(_config(tmp_path, resolved=True), environ={"PM_AGENT_ENV": "prod"})
    assert settings.strict and settings.verifier.judge_required


def test_strict_environments_refuse_a_disabled_entailment_judge(tmp_path: Path) -> None:
    """V5 is fail-closed by design; ``judge_required: false`` is a laptop convenience, never stage/prod."""
    path = _config(tmp_path, resolved=True, verifier={"judge_required": False})
    assert not load_settings(path, environ={"PM_AGENT_ENV": "dev"}).verifier.judge_required
    with pytest.raises(ConfigurationError, match="judge_required"):
        load_settings(path, environ={"PM_AGENT_ENV": "stage"})


@pytest.mark.parametrize("gazetteers", [
    {"products": ["DrillOps"]},                                           # organizations missing
    {"organizations": {"Contoso Energy": ["CNE"]}},                       # products missing
    {"organisations": {"Contoso Energy": ["CNE"]}, "products": ["X"]},    # typo'd key
    {"organizations": {}, "products": ["DrillOps"]},                      # present but empty
])
def test_strict_environments_require_the_gazetteers_grounding_reads(tmp_path: Path,
                                                                     gazetteers: dict[str, Any]) -> None:
    """Missing customer gazetteers silently disable P-04 and V4 aliasing (``.get(...) or {}``) — fail loudly."""
    path = _config(tmp_path, resolved=True, grounding={"gazetteers": gazetteers})
    load_settings(path, environ={"PM_AGENT_ENV": "local"})  # tolerated on a laptop
    with pytest.raises(ConfigurationError, match="gazetteers"):
        load_settings(path, environ={"PM_AGENT_ENV": "prod"})
    assert set(REQUIRED_GAZETTEERS) == {"organizations", "products"}


@pytest.mark.parametrize("gazetteers", [
    {"organizations": ["Contoso Energy"], "products": []},                # list instead of a mapping
    {"organizations": {"Contoso Energy": "CNE"}, "products": []},         # aliases must be a list
    {"organizations": {}, "products": "DrillOps"},                        # products must be a list
])
def test_gazetteer_shapes_are_validated_everywhere(tmp_path: Path, gazetteers: dict[str, Any]) -> None:
    with pytest.raises(ValidationError, match="gazetteers"):
        load_settings(_config(tmp_path, grounding={"gazetteers": gazetteers}), environ={"PM_AGENT_ENV": "local"})


@pytest.mark.parametrize(("section", "changes"), [
    ("verifier", {"dual_judge_action_types": ["ADO_CREATE_WORKITEM"]}),   # typo would disable dual judging
    ("correlation", {"scoring": "embeddings"}),                           # typo would fall back to lexical
    ("verifier", {"near_duplicate_threshold": 1.5}),
    ("verifier", {"max_quote_chars": MAX_QUOTE_CHARS + 1}),
    ("verifier", {"max_quote_chars": 0}),
    ("verifier", {"report_max_age_hours": 0}),
    ("correlation", {"assign_threshold": -0.1}),
    ("correlation", {"lexical_adjudicate_floor": 2}),
    ("correlation", {"adjudicate_floor": 0.9, "assign_threshold": 0.8}),  # grey zone inverted
    ("correlation", {"window_days": 0}),
    ("policies", {"min_dls_reuse_ratio": 1.2}),
    ("hitl", {"token_ttl_minutes": 0}),
])
def test_invalid_values_fail_validation_instead_of_failing_open(tmp_path: Path, section: str,
                                                                changes: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        load_settings(_config(tmp_path, **{section: changes}), environ={"PM_AGENT_ENV": "local"})


def test_max_quote_chars_shares_the_citation_schema_limit() -> None:
    """A larger setting used to crash ``check_claims`` (``Citation.quote`` is capped at ``MAX_QUOTE_CHARS``)."""
    assert VerifierSettings(version="v").max_quote_chars == MAX_QUOTE_CHARS
    with pytest.raises(ValidationError):
        VerifierSettings(version="v", max_quote_chars=MAX_QUOTE_CHARS + 1)


def test_embedded_placeholders_are_rejected_in_strict_environments(tmp_path: Path) -> None:
    """``https://PLACEHOLDER:Q-05:x/api`` is just as unresolved as a bare token (the registry tool finds both)."""
    path = _config(tmp_path, resolved=True, connectors={"pagerduty": {"api_base": "https://PLACEHOLDER:Q-07:pd_host/v2"}})
    with pytest.raises(PlaceholderNotResolved, match="Q-07"):
        load_settings(path, environ={"PM_AGENT_ENV": "prod"})
