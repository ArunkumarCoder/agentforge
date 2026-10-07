"""Create the configured LLM provider from settings."""

from __future__ import annotations

from agentforge.config import LLMCacheMode as CacheMode
from agentforge.config import LLMProvider as ProviderName
from agentforge.config import Settings
from agentforge.llm.anthropic_provider import AnthropicProvider
from agentforge.llm.base import LLMProvider
from agentforge.llm.cache import CachedProvider, DiskCache, MemoryCache
from agentforge.llm.fake import FakeProvider
from agentforge.llm.ollama_provider import OllamaProvider
from agentforge.llm.openai_provider import OpenAIProvider
from agentforge.llm.pricing import PriceTable
from agentforge.llm.retry import RetryPolicy


def create_provider(
    settings: Settings,
    *,
    retry: RetryPolicy | None = None,
    pricing: PriceTable | None = None,
) -> LLMProvider:
    """Build the provider named by ``settings.llm.provider``, wrapped in a cache if configured."""
    provider = _base_provider(
        settings, retry or RetryPolicy(max_retries=settings.llm.max_retries), pricing
    )
    return with_cache(provider, settings)


def with_cache(provider: LLMProvider, settings: Settings) -> LLMProvider:
    """Wrap ``provider`` according to ``settings.llm.cache``."""
    mode = settings.llm.cache
    if mode is CacheMode.OFF:
        return provider
    if mode is CacheMode.MEMORY:
        return CachedProvider(provider, MemoryCache())
    read_only = mode is CacheMode.REPLAY
    return CachedProvider(
        provider, DiskCache(settings.llm.cache_dir, read_only=read_only), replay=read_only
    )


def _base_provider(
    settings: Settings, retry: RetryPolicy, pricing: PriceTable | None
) -> LLMProvider:
    llm = settings.llm
    match llm.provider:
        case ProviderName.ANTHROPIC:
            key = settings.anthropic_api_key
            return AnthropicProvider(
                model=llm.model,
                api_key=key.get_secret_value() if key else None,
                timeout_s=llm.timeout_s,
                retry=retry,
                pricing=pricing,
            )
        case ProviderName.OPENAI:
            key = settings.openai_api_key
            return OpenAIProvider(
                model=llm.model,
                api_key=key.get_secret_value() if key else None,
                timeout_s=llm.timeout_s,
                retry=retry,
                pricing=pricing,
            )
        case ProviderName.OLLAMA:
            return OllamaProvider(
                model=llm.model,
                base_url=llm.ollama_base_url,
                timeout_s=llm.timeout_s,
                retry=retry,
                pricing=pricing,
            )
        case ProviderName.FAKE:
            return FakeProvider(model=llm.model, timeout_s=llm.timeout_s, pricing=pricing)
