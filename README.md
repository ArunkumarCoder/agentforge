# AgentForge

[![CI](https://github.com/ArunkumarCoder/agentforge/actions/workflows/ci.yml/badge.svg)](https://github.com/ArunkumarCoder/agentforge/actions/workflows/ci.yml)
![Python](https://img.shields.io/badge/python-3.12%20%7C%203.13-blue)
![Types: mypy strict](https://img.shields.io/badge/types-mypy%20strict-informational)
[![Ruff](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/ruff/main/assets/badge/v2.json)](https://github.com/astral-sh/ruff)

A 25-agent AI engineering platform with a multi-agent orchestrator. Give it one business requirement and it runs the right agents (Business Analyst → Product → Planner → API → Database → Security → QA → Docs) to produce a finished deliverable.

> **Status:** Week 4 in progress. Nine real agents are in: **Code Review**, **Bug Fix**, **Test Generation**, **Documentation**, **Refactoring**, **API Design**, **Database Schema**, **Security Audit** and now **Performance**, each checked against reality (patches are applied, tests are run, syntax trees are compared, specs are validated, DDL is parsed by PostgreSQL's own parser, every claimed vulnerability must quote real code, hotspots are measured with cProfile) rather than trusted. `agentforge eval run` scores their golden cases. Underneath: config, logging, `BaseAgent`, four LLM providers with cost tracking and caching, typed tools, built-in workspace/git/command/HTTP tools, the tool-calling loop and the agent registry. Next: Migration and a basic API.

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
| 6 | API Design | `api-design` | Turns requirements into an OpenAPI 3.1 spec that validates against the official schema and follows design rules (operationIds, error responses, auth, pagination) |
| 7 | Database Schema | `db-schema` | Turns entity descriptions into PostgreSQL DDL (parsed by Postgres's own parser, linted, optionally run on a real server), SQLAlchemy 2.0 models that must match it, and a Mermaid ER diagram |
| 8 | Security Audit | `security-audit` | Runs bandit and a secrets scanner, then has the model triage every finding (real or false positive, real severity, concrete fix) and add what scanners miss; each addition must quote the code, and the report is merged, de-duplicated and sorted by risk |
| 9 | Performance | `performance` | Scans Python for slow patterns (list lookups in loops, `+=` on strings, a query per iteration, `pop(0)`, nested loops), optionally profiles a script with cProfile, and has the model triage every candidate with a fix; the report is ranked by impact and measured share of run time, and names the bottleneck |

```bash
# review your own uncommitted changes (needs a real provider in .env)
uv run agentforge agents run code-review -d '{"repo_path": ".", "base": "HEAD"}'

# diagnose an error in this repo (put the traceback in error.json: {"error": "...", "repo_path": "."})
uv run agentforge agents run bug-fix -i error.json

# write tests for one module of this repo (tests are run in a temp dir; nothing is written here)
uv run agentforge agents run test-gen -d '{"repo_path": ".", "target": "src/agentforge/tools/patching.py"}'

# document a module (prints the documented source; your file is not changed)
uv run agentforge agents run doc-gen -d '{"repo_path": ".", "target": "src/agentforge/tools/patching.py", "readme": false}'

# design an API from a description (prints the spec as YAML)
uv run agentforge agents run api-design -d '{"requirements": "A to-do service: tasks have a title and a done flag; list, create, update and delete them.", "auth": "bearer", "pagination": "offset"}'

# design a PostgreSQL schema (prints DDL, SQLAlchemy models and a Mermaid ER diagram)
uv run agentforge agents run db-schema -d '{"requirements": "A library: books (title, isbn unique), members (name, email unique), and loans linking a member to a book with a due date."}'

# find slow code (add profile_script to measure with cProfile; that script is run)
uv run agentforge agents run performance -d '{"repo_path": ".", "paths": ["src"], "profile_script": "scripts/benchmark.py"}'

# audit code for vulnerabilities (read-only: bandit and a secrets scan, then triage)
uv run agentforge agents run security-audit -d '{"repo_path": ".", "paths": ["src"], "min_severity": "medium"}'

# refactor a module that has tests (prints the diff; your file is not changed)
uv run agentforge agents run refactor -d '{"repo_path": ".", "target": "src/agentforge/tools/patching.py"}'
```

The Code Review agent only trusts what it can check. Findings on files that aren't in the diff are dropped, line numbers outside the changed hunks are cleared, and the verdict always follows from the findings: any `critical` or `major` finding means `request_changes`. It ships with 5 golden eval cases (SQL injection, off-by-one, hardcoded secret, missing `await`, and a clean refactor), which `make test-live` scores against your configured model.

The Bug Fix agent never hands back a patch it hasn't tried. Its patch is applied in memory to the real files (nothing is written to disk); if it doesn't apply, or changes nothing, the model gets the exact mismatch back and one chance to fix it, and `patch_applies` in the output tells you the result. Its 5 golden cases (division by zero, missing defaults, `None` from `re.match`, an off-by-one index and a `str` + `int` concatenation) are scored by applying the patch and running a small behaviour check in a separate Python process.

The Test Generation agent only returns tests that have passed. It runs them with pytest and, on failure, sends pytest's output back to the model, up to `max_attempts` runs (default 3). The output includes the final run (tests, failures, coverage of the target) and the coverage from any existing tests you passed in. Tests run in a temporary directory with a separate `python -I`, a minimal environment (no API keys) and a timeout. That is careful, but it is not a sandbox: only run it on code you trust. Its 5 golden cases are scored on coverage and on **mutants**, small deliberate bugs the tests must catch, so tests that merely call the code do not pass.

The Documentation agent cannot change your code: the model sends docstrings by name, AgentForge places them, and the syntax trees before and after must match apart from docstrings. Every public definition must end up documented, and each new docstring must pass a rubric (every parameter described, every exception the function raises named, a Returns or Yields section where needed, a one-line summary). `>>>` examples are run as doctests. Problems go back to the model up to twice. Its 5 golden cases cover all three styles, classes, generators and a partly documented module.

The API Design agent returns a spec that has been validated, not just written. The model's YAML is checked with the official OpenAPI 3.1 schema (`openapi-spec-validator`, which also resolves every `$ref`), then linted: every operation needs a unique `operationId`, a summary, a success response and an error response; every path parameter is declared; `GET` and `DELETE` have no body; the security scheme you asked for exists and covers every endpoint (public ones say `security: []`); and every list endpoint takes your chosen pagination parameters. Problems go back to the model up to twice, with the exact location. Style findings (verb-like paths, missing tags, unused schemas) come back as `warnings` without blocking. The returned `spec` is canonical YAML. Its 5 golden cases (tasks, bookstore, accounts, documents and orders) check that every required operation, field, status code, enum and media type is in the spec, and that it does not wander beyond what was asked.

The Database Schema agent returns a schema that has been checked, not just written. The DDL goes through PostgreSQL's own parser (`pglast`), and only schema definitions are accepted (`DROP`, `INSERT`, `GRANT` and transactions are refused, so a model-written script can never change data). Then rules: every table has a primary key, every foreign key points at a real, unique, same-typed column in a table created **earlier**, and every type is a known one. The SQLAlchemy models are imported in a separate `python -I` process and must match the DDL table by table: columns, nullability, primary keys and foreign keys. The ER diagram is generated from the checked DDL, so it cannot disagree with it. Problems go back to the model up to twice. Style findings (`timestamp` without time zone, `float` for money, unindexed foreign keys) come back as `warnings`.

For the strongest check, point `AGENTFORGE_CHECK_DATABASE_URL` at a **scratch** PostgreSQL database (and have `psql` installed): the DDL is then also run on that server inside a throw-away schema and rolled back, and the output says `executed: true`. The server's own error message goes back to the model if it rejects the DDL. Without the variable the step is skipped and `executed` is `null`. CI runs these checks against a PostgreSQL 16 service. Its 5 golden cases (blog, shop, school, booking and multi-tenant SaaS) check tables, columns, foreign keys, uniqueness, `CHECK` constraints, types, nullability and enums.

The Security Audit agent never runs the code it reads. Bandit runs in a separate process on a temporary copy, and a small secrets scanner covers `.env`, YAML and JSON files (placeholders such as `changeme` are ignored, and secrets are redacted in the report). Every scanner finding is numbered `S1`, `S2`, and the model must triage **each one**: confirmed (with a concrete fix), false positive (with the reason it is safe) or needs review. A finding the scanner rates high severity with high confidence cannot be dismissed. The model may also report vulnerabilities scanners cannot see, such as path traversal, SSRF or a missing ownership check, but each one needs a quote that really appears within three lines of the cited line, and it may not repeat a scanner finding. Anything that fails goes back to the model up to twice. AgentForge then builds the report itself: merged by file, line and category, sorted by severity, with false positives listed separately and an overall `risk`.

The Performance agent reads source as syntax trees and never runs it, with one exception you control: if you pass `profile_script`, that script runs once under `cProfile` in a separate process (temporary copy, minimal environment, 60 second limit) so the report is based on measured time, not guesses. Functions with 5% or more of the run time are listed; a measured function with no static finding becomes its own candidate (`H1`), and static candidates inside a measured function carry its share. The model must triage **every** candidate (real cost, harmless here, or needs a measurement), and a candidate that took 25% or more of the run time cannot be dismissed. Slowdowns the tools cannot see, such as exponential recursion that needs caching, may be added only with a quote that appears within three lines of the cited line. The final report is merged, sorted by impact and then by measured share, and names the `bottleneck`. It does not rewrite your code; the fixes are suggestions.

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
