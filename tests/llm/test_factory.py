"""Tests for building providers from settings."""

from pathlib import Path

import pytest
from pydantic import SecretStr

from agentforge.config import LLMCacheMode, LLMSettings, Settings
from agentforge.config import LLMProvider as ProviderName
from agentforge.llm import (
    AnthropicProvider,
    CachedProvider,
    DiskCache,
    FakeProvider,
    LLMConfigError,
    MemoryCache,
    OllamaProvider,
    OpenAIProvider,
    create_provider,
)


def settings(
    provider: ProviderName, cache: LLMCacheMode = LLMCacheMode.OFF, **keys: str
) -> Settings:
    return Settings(
        llm=LLMSettings(
            provider=provider,
            model="some-model",
            timeout_s=9,
            max_retries=4,
            cache=cache,
            cache_dir=Path("recorded"),
        ),
        **{name: SecretStr(value) for name, value in keys.items()},  # type: ignore[arg-type]
    )


def test_anthropic() -> None:
    provider = create_provider(settings(ProviderName.ANTHROPIC, anthropic_api_key="k"))
    assert isinstance(provider, AnthropicProvider)
    assert provider.model == "some-model"
    assert provider.timeout_s == 9
    assert provider.retry.max_retries == 4


def test_openai() -> None:
    provider = create_provider(settings(ProviderName.OPENAI, openai_api_key="k"))
    assert isinstance(provider, OpenAIProvider)


def test_ollama_needs_no_key() -> None:
    provider = create_provider(settings(ProviderName.OLLAMA))
    assert isinstance(provider, OllamaProvider)
    assert provider.base_url == "http://localhost:11434"


def test_fake_is_the_default() -> None:
    provider = create_provider(Settings())
    assert isinstance(provider, FakeProvider)


@pytest.mark.parametrize("name", [ProviderName.ANTHROPIC, ProviderName.OPENAI])
def test_missing_key(name: ProviderName) -> None:
    with pytest.raises(LLMConfigError, match="API_KEY is not set"):
        create_provider(settings(name))


def test_memory_cache() -> None:
    provider = create_provider(settings(ProviderName.FAKE, LLMCacheMode.MEMORY))
    assert isinstance(provider, CachedProvider)
    assert isinstance(provider.cache, MemoryCache)
    assert isinstance(provider.inner, FakeProvider)


@pytest.mark.parametrize(
    ("mode", "read_only"), [(LLMCacheMode.DISK, False), (LLMCacheMode.REPLAY, True)]
)
def test_disk_and_replay_cache(mode: LLMCacheMode, read_only: bool) -> None:
    provider = create_provider(settings(ProviderName.FAKE, mode))
    assert isinstance(provider, CachedProvider)
    assert isinstance(provider.cache, DiskCache)
    assert provider.cache.directory == Path("recorded")
    assert provider.cache.read_only is read_only
    assert provider.replay is read_only
