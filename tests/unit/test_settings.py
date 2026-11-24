"""Tests for settings loading, environment overrides, strict placeholder handling and the Gen AI client contract."""
from pathlib import Path

import pytest

from pm_agent.placeholders import PlaceholderNotResolved
from pm_agent.settings import ConfigurationError, Environment, genai_client_kwargs, load_settings

REPO_CONFIG = Path(__file__).resolve().parents[2] / "config" / "pm_agent.yaml"


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
