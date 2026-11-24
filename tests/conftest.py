"""Shared pytest configuration for the reference implementation."""
import os

import pytest


@pytest.fixture(autouse=True)
def _isolate_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Tests never talk to real Google Cloud: force the local environment and strip credentials-related vars."""
    monkeypatch.setenv("PM_AGENT_ENV", "local")
    for var in ("GOOGLE_API_KEY", "GEMINI_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    os.environ.setdefault("GOOGLE_CLOUD_PROJECT", "chayan-genai-bb")
    os.environ.setdefault("GOOGLE_CLOUD_LOCATION", "us-central1")
