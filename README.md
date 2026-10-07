# AgentForge

[![CI](https://github.com/ArunkumarCoder/agentforge/actions/workflows/ci.yml/badge.svg)](https://github.com/ArunkumarCoder/agentforge/actions/workflows/ci.yml)
![Python](https://img.shields.io/badge/python-3.12%20%7C%203.13-blue)
![Types: mypy strict](https://img.shields.io/badge/types-mypy%20strict-informational)
[![Ruff](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/ruff/main/assets/badge/v2.json)](https://github.com/astral-sh/ruff)

A 25-agent AI engineering platform with a multi-agent orchestrator. Give it one business requirement and it runs the right agents (Business Analyst → Product → Planner → API → Database → Security → QA → Docs) to produce a finished deliverable.

> **Status:** Week 2 — tools. Config, logging, `BaseAgent`, four LLM providers with cost tracking and caching, typed tools and the tool-calling loop are in place; no production agents yet.

## Quick start (dev)

Requires [uv](https://docs.astral.sh/uv/). It installs Python 3.12 for you if needed.

```bash
uv sync                    # create .venv and install dependencies
uv run pre-commit install  # run lint and type checks on every commit
make check                 # lint + types + tests with coverage (what CI runs)
```

No `make`? See [CONTRIBUTING.md](CONTRIBUTING.md) for the plain `uv run` commands.

## Your first agent

```python
from agentforge.core import AgentInput, AgentOutput, BaseAgent, RunContext


class WordCountInput(AgentInput):
    text: str


class WordCountOutput(AgentOutput):
    words: int


class WordCountAgent(BaseAgent[WordCountInput, WordCountOutput]):
    name = "word-count"
    description = "Counts the words in a text."
    input_model = WordCountInput
    output_model = WordCountOutput

    async def execute(self, data: WordCountInput, ctx: RunContext) -> WordCountOutput:
        return WordCountOutput(words=len(data.text.split()))


result = WordCountAgent().run({"text": "agents all the way down"})
print(result.status, result.output)  # succeeded words=5
```

Every run returns a `RunResult`, which carries a status, the typed output or an error, usage and timing. Runs never raise, so callers handle success and failure the same way.

## Project layout

```
src/agentforge/
├── config.py        # settings (pydantic-settings)
├── log.py           # structured logging (structlog)
├── core/            # schemas, BaseAgent, errors, tool-calling loop
├── llm/             # LLMProvider, Anthropic/OpenAI/Ollama/Fake, retries, pricing, cache
└── tools/           # BaseTool, @tool decorator, ToolRegistry, permissions
tests/               # pytest suite (offline, no API keys needed)
tests/live/          # opt-in smoke tests against real APIs (make test-live)
docs/                # architecture, agent catalog, ADRs
.github/workflows/   # CI: lint, mypy, tests on Linux + Windows
```

## Calling an LLM

```python
import asyncio

from agentforge.config import get_settings
from agentforge.core import Message
from agentforge.llm import LLMRequest, create_provider

# Uses AGENTFORGE_LLM__PROVIDER / __MODEL and the provider's API key from .env
llm = create_provider(get_settings())
response = asyncio.run(llm.complete(LLMRequest(messages=[Message.user("Say hi")])))
print(response.text, response.usage)
```

Retryable failures (429, 5xx, timeouts, connection errors) are retried with exponential backoff and jitter, and a server's `Retry-After` hint is respected. Everything else raises a typed `LLMError` straight away. Run `make test-live` to check your keys against the real APIs.

| Provider | `AGENTFORGE_LLM__PROVIDER` | Needs | Cost |
|---|---|---|---|
| Anthropic | `anthropic` | `ANTHROPIC_API_KEY` | from `llm/pricing.py` |
| OpenAI | `openai` | `OPENAI_API_KEY` | from `llm/pricing.py` |
| Ollama (local) | `ollama` | `ollama pull <model>` and a running server | free |
| Fake (offline) | `fake` *(default)* | nothing | free |

Every response carries `usage.input_tokens`, `usage.output_tokens` and `usage.cost_usd`. Prices are per million tokens and live in `src/agentforge/llm/pricing.py`, so check them against the providers' pricing pages.

### Testing without API keys

- **`FakeProvider`** follows a script: plain text, tool calls, errors or a function of the request. Without a script it simply echoes the input.
- **Record and replay:** run once with a real provider and `AGENTFORGE_LLM__CACHE=disk` to record responses as JSON files. Later runs with `AGENTFORGE_LLM__CACHE=replay` serve those files and **fail rather than call the API** when a response is missing, which makes them safe for CI.
- `AGENTFORGE_LLM__CACHE=memory` or `disk` also saves money while you iterate on prompts: identical requests are answered from the cache with zero usage.

## Tools and the tool-calling loop

```python
from typing import Annotated

from pydantic import Field

from agentforge.config import get_settings
from agentforge.core import AgentInput, AgentOutput, BaseAgent, Message, RunContext
from agentforge.core.loop import run_tool_loop
from agentforge.llm import create_provider
from agentforge.tools import ToolRegistry, tool


@tool
def get_weather(city: Annotated[str, Field(description="City name")]) -> str:
    """Get the current weather for a city."""
    return f"31°C and humid in {city}"


class Question(AgentInput):
    text: str


class Answer(AgentOutput):
    answer: str


class WeatherAgent(BaseAgent[Question, Answer]):
    name = "weather"
    description = "Answers weather questions using a tool."
    input_model = Question
    output_model = Answer

    async def execute(self, data: Question, ctx: RunContext) -> Answer:
        result = await run_tool_loop(
            create_provider(get_settings()),
            [Message.user(data.text)],
            ctx,
            tools=ToolRegistry([get_weather]),
            max_steps=5,
        )
        return Answer(answer=result.text)
```

- **Typed arguments:** a tool's arguments are a Pydantic model, built from the function signature when you use `@tool`. Its JSON Schema is what the model sees, and the model's arguments are checked against it before the tool runs.
- **Tools never crash the run:** bad arguments, unknown tools, timeouts and exceptions all go back to the model as error results, so it can correct itself. Internal exception details are logged but never shown to the model.
- **Permissions:** each tool declares `read`, `write`, `network` or `exec`. The loop allows only `read` unless you pass `allowed=...`.
- **Guards:** `max_steps` caps the number of LLM calls (`ToolLoopLimitError` beyond it), each tool has a timeout, and long outputs are truncated. Token usage and cost from every step are added to the run.

## Configuration

Settings live in `src/agentforge/config.py` and are read from environment variables, then `.env.<env>`, then `.env`, then built-in defaults. Copy `.env.example` to `.env` to get started. Nested settings use a double underscore, e.g. `AGENTFORGE_LLM__MODEL`.

| Variable | Default | Description |
|---|---|---|
| `AGENTFORGE_ENV` | `dev` | Profile: `dev`, `test` or `prod`. Also loads `.env.<env>` |
| `AGENTFORGE_LOGGING__LEVEL` | `INFO` | `DEBUG`, `INFO`, `WARNING`, `ERROR`, `CRITICAL` |
| `AGENTFORGE_LOGGING__FORMAT` | `console` | `console` (human-readable) or `json` (CI and production) |
| `AGENTFORGE_LLM__PROVIDER` | `fake` | `anthropic`, `openai`, `ollama` or `fake` (offline) |
| `AGENTFORGE_LLM__MODEL` | `fake-model` | Model name for the chosen provider |
| `AGENTFORGE_LLM__TIMEOUT_S` | `60` | Request timeout in seconds (> 0) |
| `AGENTFORGE_LLM__MAX_RETRIES` | `3` | Retries on transient errors (0–10) |
| `AGENTFORGE_LLM__OLLAMA_BASE_URL` | `http://localhost:11434` | Local Ollama server |
| `AGENTFORGE_LLM__CACHE` | `off` | `off`, `memory`, `disk` or `replay` |
| `AGENTFORGE_LLM__CACHE_DIR` | `.cache/llm` | Where `disk`/`replay` keep recorded responses |
| `ANTHROPIC_API_KEY` | — | Anthropic key (`AGENTFORGE_ANTHROPIC_API_KEY` also works) |
| `OPENAI_API_KEY` | — | OpenAI key (`AGENTFORGE_OPENAI_API_KEY` also works) |

Logs go to stderr. Inside `run_context()`, every log line carries `run_id` and `correlation_id`, and secret-looking fields (`api_key`, `token`, `password`, `authorization`, …) are replaced with `***`.

```python
from agentforge.config import get_settings
from agentforge.log import configure_logging, get_logger, run_context

configure_logging(get_settings())
log = get_logger(__name__)
with run_context():
    log.info("agent.started", agent="code-review")
```

## Documentation

- [Architecture](docs/ARCHITECTURE.md)
- [Agent catalog](docs/AGENTS.md)
- [Architecture decision records](docs/adr/)

## Roadmap

| Milestone | Target | Scope |
|---|---|---|
| M1 — Portfolio MVP (v0.1.0) | 30 Oct 2026 | Core runtime, CLI, 10 engineering agents, evals, basic API |
| M2 — Serious AI project (v0.2.0) | 27 Nov 2026 | Research, data, RAG, business and delivery agents (24 total) |
| M3 — Flagship (v1.0.0) | 01 Jan 2027 | Orchestrator, memory, permissions, observability, dashboard, docs |
