"""Shared pytest configuration for the reference implementation."""
import os

import pytest
from hypothesis import HealthCheck, settings

# Property-based tests must be reproducible in CI: fixed seed derivation, no wall-clock deadlines (shared runners are
# noisy). ``HYPOTHESIS_PROFILE=dev`` explores more examples locally.
settings.register_profile("ci", derandomize=True, deadline=None, max_examples=150,
                          suppress_health_check=[HealthCheck.too_slow])
settings.register_profile("dev", deadline=None, max_examples=1000)
settings.load_profile(os.environ.get("HYPOTHESIS_PROFILE", "ci"))


@pytest.fixture(autouse=True)
def _isolate_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Tests never talk to real Google Cloud: force the local environment and strip credentials-related vars."""
    monkeypatch.setenv("PM_AGENT_ENV", "local")
    for var in ("GOOGLE_API_KEY", "GEMINI_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    os.environ.setdefault("GOOGLE_CLOUD_PROJECT", "chayan-genai-bb")
    os.environ.setdefault("GOOGLE_CLOUD_LOCATION", "us-central1")
