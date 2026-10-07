# ADR-0002: Own agent runtime and LLM layer instead of LangChain

- **Status:** Accepted
- **Date:** 2026-10-01
- **Deciders:** Arunkumar

## Context

Agents 1–24 need the same few things: call an LLM, let it use tools, validate a structured result, and report tokens, cost and errors. Frameworks such as LangChain, LlamaIndex or the vendors' agent SDKs provide this out of the box. The alternative is a small runtime of our own (`BaseAgent` from Day 4, `LLMProvider` from Day 6) over the official Anthropic and OpenAI SDKs.

Forces:

- **Learning and portfolio value.** The project is meant to show that I understand tool calling, retries, structured output and evaluation, not only that I can configure a framework.
- **Control over failure behaviour.** Retries, timeouts and error kinds must be explicit, logged and testable offline.
- **Stability.** 25 agents will depend on this layer for 14 weeks; framework API churn would cost refactoring time.
- **Effort.** Writing our own layer costs time up front (weeks 1–2).

## Decision

1. Agents 1–24 run on our own `BaseAgent` and tool-calling loop.
2. LLM access goes through our own `LLMProvider` interface, with one thin adapter per vendor over the **official SDKs** (`anthropic`, `openai`). SDK retries are disabled (`max_retries=0`); `LLMProvider` owns retries, backoff and the per-attempt timeout.
3. Vendor exceptions are mapped to a small provider-neutral hierarchy (`LLMRateLimitError`, `LLMServerError`, `LLMAuthError`, ...). Only errors marked `retryable` are retried.
4. LangGraph is still the planned choice for the Agent 25 orchestrator (ADR-0003, week 10), where a graph runtime with checkpointing adds real value.

## Alternatives considered

- **LangChain for everything:** fastest start and many integrations, but it hides exactly the mechanics this project should demonstrate, adds several layers of abstraction when debugging, and has a history of breaking API changes.
- **A thin multi-provider library (e.g. LiteLLM):** one call signature for many vendors, but it is another dependency that sits between us and the SDKs, and it reduces control over error mapping and retries. We can still add it later as one more adapter.
- **Vendor agent SDKs:** excellent for one vendor, but they lock the runtime to that vendor.

## Consequences

- **Positive:** small surface area (about 500 lines) that is fully typed, tested offline against the real SDKs through an in-memory HTTP transport, and logged consistently. New providers are one adapter each (Ollama and Fake in Day 7).
- **Negative:** we maintain the message conversion for each vendor ourselves (for example Anthropic's alternating turns and `tool_result` blocks). New vendor features, such as streaming or prompt caching, have to be added by hand.
- **Follow-ups:** Day 7 adds Ollama, a FakeLLM, cost tracking and a response cache; Day 8 adds the tool-calling loop on top of `LLMRequest.tools` and `LLMResponse.message.tool_calls`.
