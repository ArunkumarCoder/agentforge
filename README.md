# AgentForge

[![CI](https://github.com/ArunkumarCoder/agentforge/actions/workflows/ci.yml/badge.svg)](https://github.com/ArunkumarCoder/agentforge/actions/workflows/ci.yml)
![Python](https://img.shields.io/badge/python-3.12%20%7C%203.13-blue)
![Types: mypy strict](https://img.shields.io/badge/types-mypy%20strict-informational)
[![Ruff](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/ruff/main/assets/badge/v2.json)](https://github.com/astral-sh/ruff)

A 25-agent AI engineering platform with a multi-agent orchestrator. Give it one business requirement and it runs the right agents (Business Analyst → Product → Planner → API → Database → Security → QA → Docs) to produce a finished deliverable.

> **Status:** Week 1 — foundation. Config, structured logging, core schemas and `BaseAgent` are in place; no LLM-backed agents yet.

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
└── core/            # schemas, BaseAgent, errors
tests/               # pytest suite (offline, no API keys needed)
docs/                # architecture, agent catalog, ADRs
.github/workflows/   # CI: lint, mypy, tests on Linux + Windows
```

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
