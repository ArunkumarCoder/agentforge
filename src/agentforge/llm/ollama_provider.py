"""Ollama adapter for local models (llama3.2, qwen2.5, mistral, ...).

Ollama serves an OpenAI-compatible API at ``<base_url>/v1``, so this reuses
the OpenAI adapter with a different base URL, a placeholder API key and
friendlier errors. Local models are free, so cost is always 0.

Setup: install Ollama, then ``ollama pull llama3.2`` and make sure
``ollama serve`` is running (the desktop app does this for you).
"""

from __future__ import annotations

from typing import ClassVar

import openai

from agentforge.llm.errors import LLMBadRequestError, LLMConnectionError, LLMError
from agentforge.llm.openai_provider import OpenAIProvider, map_openai_error
from agentforge.llm.pricing import PriceTable
from agentforge.llm.retry import RetryPolicy

DEFAULT_OLLAMA_URL = "http://localhost:11434"


class OllamaProvider(OpenAIProvider):
    name = "ollama"
    is_local: ClassVar[bool] = True

    def __init__(
        self,
        *,
        model: str,
        base_url: str = DEFAULT_OLLAMA_URL,
        timeout_s: float = 120.0,
        retry: RetryPolicy | None = None,
        pricing: PriceTable | None = None,
        client: openai.AsyncOpenAI | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        super().__init__(
            model=model,
            # Ollama ignores the key, but the OpenAI SDK requires one.
            api_key="ollama",
            base_url=f"{self.base_url}/v1",
            timeout_s=timeout_s,
            retry=retry,
            pricing=pricing,
            client=client,
        )

    def _map_error(self, exc: openai.APIError) -> LLMError:
        error = map_openai_error(exc, self.name)
        if isinstance(error, LLMConnectionError):
            return LLMConnectionError(
                f"cannot reach Ollama at {self.base_url}. Is it running? (`ollama serve`)",
                provider=self.name,
            )
        if isinstance(error, LLMBadRequestError) and error.status_code == 404:
            return LLMBadRequestError(
                f"model {self.model!r} not found in Ollama. Run `ollama pull {self.model}`",
                provider=self.name,
                status_code=404,
            )
        return error
