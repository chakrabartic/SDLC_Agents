"""Tests for the placeholder mechanism (reference-architecture rule: unknown SLB details are tracked, never guessed)."""
import pytest

from pm_agent.placeholders import (
    PlaceholderNotImplemented,
    PlaceholderNotResolved,
    find_placeholders,
    is_placeholder,
    not_implemented,
    parse_placeholder,
    placeholder,
    require,
)


def test_placeholder_token_round_trip() -> None:
    token = placeholder("Q-10", "gcp_org_id")
    assert token == "PLACEHOLDER:Q-10:gcp_org_id"
    assert is_placeholder(token)
    assert parse_placeholder(token) == ("Q-10", "gcp_org_id")


@pytest.mark.parametrize("value", ["chayan-genai-bb", "", 42, None, "PLACEHOLDER:bad", "placeholder:Q-10:x"])
def test_non_placeholders_are_not_detected(value: object) -> None:
    assert not is_placeholder(value)
    assert parse_placeholder(value) is None


@pytest.mark.parametrize("qid,name", [("Q10", "x"), ("Q-1", "x"), ("Q-10", "Bad-Name"), ("Q-10", "")])
def test_invalid_placeholder_definitions_rejected(qid: str, name: str) -> None:
    with pytest.raises(ValueError):
        placeholder(qid, name)


def test_require_passes_real_values_and_tolerates_placeholders_when_not_strict() -> None:
    assert require("us-central1", strict=True) == "us-central1"
    token = placeholder("Q-09", "fallback_endpoint")
    assert require(token, strict=False) == token


def test_require_raises_in_strict_mode_with_question_id() -> None:
    with pytest.raises(PlaceholderNotResolved) as exc:
        require(placeholder("Q-01", "kb_backend"), strict=True)
    assert "Q-01" in str(exc.value)
    assert exc.value.question_id == "Q-01"


def test_find_placeholders_walks_nested_config() -> None:
    config = {
        "gcp": {"project_id": "p", "org_id": placeholder("Q-10", "gcp_org_id")},
        "connectors": [{"name": "kb", "backend": placeholder("Q-01", "kb_backend")}],
    }
    found = find_placeholders(config)
    assert ("gcp.org_id", "Q-10", "gcp_org_id") in found
    assert ("connectors[0].backend", "Q-01", "kb_backend") in found
    assert len(found) == 2


def test_not_implemented_raises_typed_error() -> None:
    with pytest.raises(PlaceholderNotImplemented) as exc:
        not_implemented("Q-05", "ServiceNow Table API call")
    assert isinstance(exc.value, NotImplementedError)
    assert "Q-05" in str(exc.value)
