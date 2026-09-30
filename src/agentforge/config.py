"""Application settings.

Values are resolved in this order (highest priority first):

1. Keyword arguments passed to ``Settings(...)`` (used in tests)
2. Environment variables (``AGENTFORGE_*``; nested fields use ``__``)
3. ``.env.<env>`` profile file, e.g. ``.env.test``
4. ``.env`` file
5. Defaults defined below

The active profile comes from ``AGENTFORGE_ENV`` (default: ``dev``).
"""

from __future__ import annotations

import os
from enum import StrEnum
from functools import lru_cache
from typing import Any

from pydantic import AliasChoices, BaseModel, Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

ENV_PREFIX = "AGENTFORGE_"


class Environment(StrEnum):
    DEV = "dev"
    TEST = "test"
    PROD = "prod"


class LogFormat(StrEnum):
    CONSOLE = "console"
    JSON = "json"


class LLMProvider(StrEnum):
    ANTHROPIC = "anthropic"
    OPENAI = "openai"
    OLLAMA = "ollama"
    FAKE = "fake"


class LoggingSettings(BaseModel):
    level: str = "INFO"
    format: LogFormat = LogFormat.CONSOLE

    @field_validator("level")
    @classmethod
    def _normalise_level(cls, value: str) -> str:
        level = value.upper()
        allowed = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
        if level not in allowed:
            raise ValueError(f"log level must be one of {sorted(allowed)}, got {value!r}")
        return level


class LLMSettings(BaseModel):
    provider: LLMProvider = LLMProvider.FAKE
    model: str = "fake-model"
    timeout_s: float = Field(default=60.0, gt=0)
    max_retries: int = Field(default=3, ge=0, le=10)
    ollama_base_url: str = "http://localhost:11434"


class Settings(BaseSettings):
    """All runtime configuration for AgentForge."""

    model_config = SettingsConfigDict(
        env_prefix=ENV_PREFIX,
        env_nested_delimiter="__",
        env_file=(".env",),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    env: Environment = Environment.DEV
    logging: LoggingSettings = Field(default_factory=LoggingSettings)
    llm: LLMSettings = Field(default_factory=LLMSettings)

    # Provider keys accept the providers' standard variable names as well.
    anthropic_api_key: SecretStr | None = Field(
        default=None,
        validation_alias=AliasChoices(f"{ENV_PREFIX}ANTHROPIC_API_KEY", "ANTHROPIC_API_KEY"),
    )
    openai_api_key: SecretStr | None = Field(
        default=None,
        validation_alias=AliasChoices(f"{ENV_PREFIX}OPENAI_API_KEY", "OPENAI_API_KEY"),
    )

    @field_validator("anthropic_api_key", "openai_api_key", mode="before")
    @classmethod
    def _blank_key_is_none(cls, value: object) -> object:
        """Treat ``ANTHROPIC_API_KEY=`` (empty, as in .env.example) as not set."""
        return None if isinstance(value, str) and not value.strip() else value

    @property
    def is_prod(self) -> bool:
        return self.env is Environment.PROD


def env_files_for(env: str) -> tuple[str, ...]:
    """Return the dotenv files to load, lowest priority first."""
    return (".env", f".env.{env}")


def load_settings(**overrides: Any) -> Settings:
    """Build settings, loading ``.env`` and the ``.env.<env>`` profile file."""
    env = os.environ.get(f"{ENV_PREFIX}ENV", Environment.DEV.value)
    return Settings(_env_file=env_files_for(env), **overrides)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Cached settings for application code. Tests call ``get_settings.cache_clear()``."""
    return load_settings()
