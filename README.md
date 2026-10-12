# AgentForge

[![CI](https://github.com/ArunkumarCoder/agentforge/actions/workflows/ci.yml/badge.svg)](https://github.com/ArunkumarCoder/agentforge/actions/workflows/ci.yml)
![Python](https://img.shields.io/badge/python-3.12%20%7C%203.13-blue)
![Types: mypy strict](https://img.shields.io/badge/types-mypy%20strict-informational)
[![Ruff](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/ruff/main/assets/badge/v2.json)](https://github.com/astral-sh/ruff)

A 25-agent AI engineering platform with a multi-agent orchestrator. Give it one business requirement and it runs the right agents (Business Analyst → Product → Planner → API → Database → Security → QA → Docs) to produce a finished deliverable.

> **Status:** Week 3 complete. Five real agents are in: **Code Review**, **Bug Fix**, **Test Generation**, **Documentation** and **Refactoring**, each checked against reality (patches are applied, tests are run, syntax trees are compared) rather than trusted. A minimal eval runner scores their golden cases with `agentforge eval run`. Underneath: config, logging, `BaseAgent`, four LLM providers with cost tracking and caching, typed tools, built-in workspace/git/command/HTTP tools, the tool-calling loop and the agent registry. Week 4 adds agents 6–10 and a basic API.

## Quick start (dev)

Requires [uv](https://docs.astral.sh/uv/). It installs Python 3.12 for you if needed.

```bash
uv sync                    # create .venv and install dependencies
uv run pre-commit install  # run lint and type checks on every commit
make check                 # lint + types + tests with coverage (what CI runs)
```

No `make`? See [CONTRIBUTING.md](CONTRIBUTING.md) for the plain `uv run` commands.

## Command line

```bash
uv run agentforge agents list                               # what's available
uv run agentforge agents show echo                          # description + input/output schema
uv run agentforge agents run echo -d '{"text": "hi"}'       # run once (pretty output)
uv run agentforge agents run echo -i input.json --json      # full RunResult as JSON (also: -i - for stdin)
uv run agentforge llm ping                                  # check provider, key, tokens and cost
uv run agentforge config show                               # effective settings (keys shown as set/not set)
uv run agentforge -v ...                                    # show info logs (quiet by default)
```

Results go to stdout and logs to stderr, so `--json` output can be piped into `jq`. Exit codes: `0` success, `1` the run failed, `2` bad input or usage. `python -m agentforge` works too.

On Windows PowerShell, quote JSON like this: `-d '{\"text\": \"hi\"}'`, or put it in a file and use `-i input.json`.

## Agents

| # | Agent | Name | What it does |
|---|---|---|---|
| 1 | Code Review | `code-review` | Reviews a diff, or a repo's uncommitted changes, and returns prioritised findings (severity, category, file:line, fix) and a verdict |
| 2 | Bug Fix | `bug-fix` | Takes an error (traceback) plus the files or a repo path, and returns the root cause, a confidence level and a unified-diff patch that is checked to apply |
| 3 | Test Generation | `test-gen` | Writes a pytest file for a module, runs it, feeds failures back until the tests pass, and reports coverage before and after |
| 4 | Documentation | `doc-gen` | Writes docstrings (Google, NumPy or Sphinx style) and a usage guide for a module, without changing any code; runs the `>>>` examples |
| 5 | Refactoring | `refactor` | Finds code smells (deep nesting, long or complex functions, duplicate blocks, long parameter lists), rewrites a module to remove them, and only returns it if the public API is unchanged and the tests still pass |

```bash
# review your own uncommitted changes (needs a real provider in .env)
uv run agentforge agents run code-review -d '{"repo_path": ".", "base": "HEAD"}'

# diagnose an error in this repo (put the traceback in error.json: {"error": "...", "repo_path": "."})
uv run agentforge agents run bug-fix -i error.json

# write tests for one module of this repo (tests are run in a temp dir; nothing is written here)
uv run agentforge agents run test-gen -d '{"repo_path": ".", "target": "src/agentforge/tools/patching.py"}'

# document a module (prints the documented source; your file is not changed)
uv run agentforge agents run doc-gen -d '{"repo_path": ".", "target": "src/agentforge/tools/patching.py", "readme": false}'

# refactor a module that has tests (prints the diff; your file is not changed)
uv run agentforge agents run refactor -d '{"repo_path": ".", "target": "src/agentforge/tools/patching.py"}'
```

The Code Review agent only trusts what it can check. Findings on files that aren't in the diff are dropped, line numbers outside the changed hunks are cleared, and the verdict always follows from the findings: any `critical` or `major` finding means `request_changes`. It ships with 5 golden eval cases (SQL injection, off-by-one, hardcoded secret, missing `await`, and a clean refactor), which `make test-live` scores against your configured model.

The Bug Fix agent never hands back a patch it hasn't tried. Its patch is applied in memory to the real files (nothing is written to disk); if it doesn't apply, or changes nothing, the model gets the exact mismatch back and one chance to fix it, and `patch_applies` in the output tells you the result. Its 5 golden cases (division by zero, missing defaults, `None` from `re.match`, an off-by-one index and a `str` + `int` concatenation) are scored by applying the patch and running a small behaviour check in a separate Python process.

The Test Generation agent only returns tests that have passed. It runs them with pytest and, on failure, sends pytest's output back to the model, up to `max_attempts` runs (default 3). The output includes the final run (tests, failures, coverage of the target) and the coverage from any existing tests you passed in. Tests run in a temporary directory with a separate `python -I`, a minimal environment (no API keys) and a timeout. That is careful, but it is not a sandbox: only run it on code you trust. Its 5 golden cases are scored on coverage and on **mutants**, small deliberate bugs the tests must catch, so tests that merely call the code do not pass.

The Documentation agent cannot change your code: the model sends docstrings by name, AgentForge places them, and the syntax trees before and after must match apart from docstrings. Every public definition must end up documented, and each new docstring must pass a rubric (every parameter described, every exception the function raises named, a Returns or Yields section where needed, a one-line summary). `>>>` examples are run as doctests. Problems go back to the model up to twice. Its 5 golden cases cover all three styles, classes, generators and a partly documented module.

The Refactoring agent changes how code is written, never what it does. It measures smells with the syntax tree first (and skips the model when there are none), refuses to touch a module without tests unless you pass `allow_untested`, and runs the tests before the model sees anything. Then it holds the rewrite to four checks: it parses, every public name keeps its signature, the smells actually went down, and the tests still pass. A failed check goes back to the model with the details, up to twice. Its 5 golden cases are scored with the same checks plus hidden tests the model never sees, so a rewrite that quietly changes behaviour fails.

### Evals

```bash
uv run agentforge eval list                    # suites and their cases
uv run agentforge eval run refactor            # one agent, real model, real tokens
uv run agentforge eval run all --concurrency 4 # every suite
make eval                                      # same as `eval run all`
```

`eval run` refuses the fake provider (it cannot solve real cases), writes a JSON report per suite to `.cache/evals/`, prints pass rate, mean score, tokens and cost, and exits 1 when a suite falls below `--min-pass-rate` (default 0.8). A crashed agent or scorer counts as an error for that case, not for the whole run.

See [docs/AGENT_TEMPLATE.md](docs/AGENT_TEMPLATE.md) for how to write a new agent.

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
├── agents/          # the agents themselves (auto-discovered); echo for smoke tests
├── cli/             # `agentforge` command (Typer + Rich)
├── core/            # schemas, BaseAgent, errors, tool-calling loop, agent registry
├── llm/             # LLMProvider, Anthropic/OpenAI/Ollama/Fake, retries, pricing, cache
└── tools/           # BaseTool, @tool, ToolRegistry, permissions
    └── builtin/     # read_file, list_files, search_text, git_diff, run_command, http_fetch
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

### Built-in tools

| Tool | Permission | What it does | Safety |
|---|---|---|---|
| `read_file` | read | Numbered lines with paging | Workspace only; no `.env`, keys, `.git/`, binaries, >1 MB |
| `list_files` | read | Glob listing | Skips `.venv`, `node_modules`, caches and secrets |
| `search_text` | read | `path:line: text` matches, text or regex | Same rules as `list_files` |
| `git_diff` | read | Diff against a ref, staged or not, limited to paths | Ref validated (no option injection), no shell |
| `run_command` | exec | Runs **developer-approved** commands only | Model chooses a name, never the program; arguments validated; timeout; output tail |
| `http_fetch` | network | Web page as text | Blocks localhost, private, link-local and metadata IPs, including after redirects; size cap; optional host allow-list |

```python
from agentforge.tools.builtin import Workspace, workspace_tools

tools = workspace_tools(
    Workspace("."),
    commands={"pytest": ["uv", "run", "pytest", "-q"], "ruff": ["uv", "run", "ruff", "check"]},
)
```

### Agent registry

Agents decorate their class with `@register_agent`. `registry.discover()` imports everything under `agentforge.agents`, after which `registry.get("echo")`, `registry.create(...)` and `registry.cards()` work by name. The CLI (next) and the orchestrator build on this.

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
