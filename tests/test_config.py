"""Tests for settings resolution and validation."""

import os
from pathlib import Path

import pytest
from pydantic import ValidationError

from agentforge.config import (
    Environment,
    LLMProvider,
    LogFormat,
    Settings,
    get_settings,
    load_settings,
)


@pytest.fixture(autouse=True)
def isolated_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Run each test in an empty folder with no AGENTFORGE_* or provider key variables."""
    for name in list(os.environ):
        if name.startswith("AGENTFORGE_") or name in {"ANTHROPIC_API_KEY", "OPENAI_API_KEY"}:
            monkeypatch.delenv(name, raising=False)
    monkeypatch.chdir(tmp_path)
    get_settings.cache_clear()


def test_defaults() -> None:
    s = load_settings()
    assert s.env is Environment.DEV
    assert s.logging.level == "INFO"
    assert s.logging.format is LogFormat.CONSOLE
    assert s.llm.provider is LLMProvider.FAKE
    assert s.anthropic_api_key is None
    assert s.is_prod is False


def test_env_var_overrides_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENTFORGE_LOGGING__LEVEL", "debug")
    monkeypatch.setenv("AGENTFORGE_LLM__MAX_RETRIES", "5")
    s = load_settings()
    assert s.logging.level == "DEBUG"
    assert s.llm.max_retries == 5


def test_precedence_env_var_over_profile_over_dotenv(monkeypatch: pytest.MonkeyPatch) -> None:
    Path(".env").write_text("AGENTFORGE_LLM__MODEL=from-dotenv\nAGENTFORGE_LLM__TIMEOUT_S=10\n")
    Path(".env.test").write_text("AGENTFORGE_LLM__MODEL=from-profile\n")
    monkeypatch.setenv("AGENTFORGE_ENV", "test")

    s = load_settings()
    assert s.env is Environment.TEST
    assert s.llm.model == "from-profile"  # profile beats .env
    assert s.llm.timeout_s == 10  # .env still applies where the profile is silent

    monkeypatch.setenv("AGENTFORGE_LLM__MODEL", "from-env-var")
    assert load_settings().llm.model == "from-env-var"  # env var beats both files


def test_profile_file_ignored_for_other_env(monkeypatch: pytest.MonkeyPatch) -> None:
    Path(".env.prod").write_text("AGENTFORGE_LLM__MODEL=prod-model\n")
    monkeypatch.setenv("AGENTFORGE_ENV", "dev")
    assert load_settings().llm.model == "fake-model"


def test_init_kwargs_have_highest_priority(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENTFORGE_ENV", "prod")
    s = load_settings(env=Environment.TEST)
    assert s.env is Environment.TEST


def test_standard_provider_key_names_are_accepted(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-123")
    s = load_settings()
    assert s.anthropic_api_key is not None
    assert s.anthropic_api_key.get_secret_value() == "sk-ant-123"
    assert "sk-ant-123" not in repr(s)  # SecretStr hides the value


def test_blank_api_key_is_treated_as_missing() -> None:
    Path(".env").write_text("ANTHROPIC_API_KEY=\nOPENAI_API_KEY=  \n")
    s = load_settings()
    assert s.anthropic_api_key is None
    assert s.openai_api_key is None


@pytest.mark.parametrize(
    ("var", "value"),
    [
        ("AGENTFORGE_LOGGING__LEVEL", "LOUD"),
        ("AGENTFORGE_LLM__TIMEOUT_S", "0"),
        ("AGENTFORGE_LLM__MAX_RETRIES", "99"),
        ("AGENTFORGE_LLM__PROVIDER", "unknown"),
        ("AGENTFORGE_ENV", "staging"),
    ],
)
def test_invalid_values_are_rejected(monkeypatch: pytest.MonkeyPatch, var: str, value: str) -> None:
    monkeypatch.setenv(var, value)
    with pytest.raises(ValidationError):
        Settings()


def test_get_settings_is_cached(monkeypatch: pytest.MonkeyPatch) -> None:
    first = get_settings()
    monkeypatch.setenv("AGENTFORGE_LLM__MODEL", "changed")
    assert get_settings() is first
    get_settings.cache_clear()
    assert get_settings().llm.model == "changed"
