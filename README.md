# AgentForge

[![CI](https://github.com/ArunkumarCoder/agentforge/actions/workflows/ci.yml/badge.svg)](https://github.com/ArunkumarCoder/agentforge/actions/workflows/ci.yml)
![Python](https://img.shields.io/badge/python-3.12%20%7C%203.13-blue)
![Types: mypy strict](https://img.shields.io/badge/types-mypy%20strict-informational)
[![Ruff](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/ruff/main/assets/badge/v2.json)](https://github.com/astral-sh/ruff)

A 25-agent AI engineering platform with a multi-agent orchestrator. Give it one business requirement and it runs the right agents (Business Analyst → Product → Planner → API → Database → Security → QA → Docs) to produce a finished deliverable.

> **Status:** v0.1.0 ([changelog](CHANGELOG.md)). Week 5 complete ([review](docs/reviews/week-05.md); release checklist in [docs/RELEASE.md](docs/RELEASE.md)). Ten real agents are in, so the engineering agents are complete: **Code Review**, **Bug Fix**, **Test Generation**, **Documentation**, **Refactoring**, **API Design**, **Database Schema**, **Security Audit**, **Performance** and now **Migration**, each checked against reality (patches are applied, tests are run, syntax trees are compared, specs are validated, DDL is parsed by PostgreSQL's own parser, every claimed vulnerability must quote real code, hotspots are measured with cProfile, migrations must keep the tests green) rather than trusted. `agentforge eval run` scores their golden cases. Underneath: config, logging, `BaseAgent`, four LLM providers with cost tracking and caching, typed tools, built-in workspace/git/command/HTTP tools, the tool-calling loop and the agent registry. An **HTTP API** now serves all of them (`agentforge serve`). Run history (SQLite or PostgreSQL) and eval baselines with regression checks are in. Code the agents run is now limited (CPU, memory, file size, output, process-group kill; see [docs/SECURITY.md](docs/SECURITY.md)). `agentforge doctor` checks an installation in one command, and `make package` proves the built wheel installs and runs on its own. Week 6 has started with agent 11, **Research** (answers with citations that are checked against the pages it really read). Next: the first measured baseline against a real model, then tag `v0.1.0` (steps in [docs/RELEASE.md](docs/RELEASE.md)).

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
uv run agentforge agents example code-review                # a ready-made input for any agent (its golden eval case)
uv run agentforge agents run code-review --example          # ...or run it straight away (needs a real model)
uv run agentforge runs list                                 # saved run history (needs AGENTFORGE_STORAGE__URL)
uv run agentforge runs stats                                # per-agent runs, success rate, tokens and cost
uv run agentforge llm ping                                  # check provider, key, tokens and cost
uv run agentforge doctor                                    # is this install ready? (add --ping for a live model call)
uv run agentforge config show                               # effective settings (keys shown as set/not set)
uv run agentforge serve                                     # HTTP API on http://127.0.0.1:8000 (docs at /docs)
uv run agentforge -v ...                                    # show info logs (quiet by default)
```

**Try any agent without writing its input.** `agents example <name>` prints a valid input as JSON, taken from the agent's golden eval cases (`--list` shows the cases, `-c NAME` picks one), and `agents run <name> --example` runs it. So `agentforge agents example security-audit | agentforge agents run security-audit -i -` works for every agent. A test checks that every example is accepted by its agent's input model, so these never go stale.

`agentforge doctor` is the first thing to run on a new machine or after editing `.env`. It checks, offline, that the Python version and packages are right, that the configured model has its key and a known price (an unpriced model reports every cost as $0.00), that the run database opens, what the sandbox limits are, whether the API would be exposed without a key, that git and psql are present, that every agent has eval cases, and whether baselines are recorded. Warnings do not fail it; failures exit 1. `--ping` also sends one short request to the model.

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
| 10 | Migration | `migration` | Brings Python code up to 3.12: finds deprecated and removed APIs (typing generics, `utcnow`, `collections` ABCs, unittest aliases, `imp`, `getargspec` ...), plans the steps, rewrites the files, and only returns them if every site is gone, the public API is unchanged and the tests pass |
| 11 | Research | `research` | Answers a question from documents, web pages and (with a search key) web search, with numbered citations. Each citation must name a source that was really read and quote it word for word, or the answer is sent back; if the sources do not say, it answers `confidence: none` instead of guessing |

```bash
# review your own uncommitted changes (needs a real provider in .env)
uv run agentforge agents run code-review -d '{"repo_path": ".", "base": "HEAD"}'

# diagnose an error in this repo (put the traceback in error.json: {"error": "...", "repo_path": "."})
uv run agentforge agents run bug-fix -i error.json

# answer from your own documents or pages, with checked citations (no key needed)
uv run agentforge agents run research -d '{"question": "What is the rate limit?", "urls": ["https://example.com/docs/limits"]}'
# add "search": true (and AGENTFORGE_RESEARCH__SEARCH_API_KEY) to let it search the web

# write tests for one module of this repo (tests are run in a temp dir; nothing is written here)
uv run agentforge agents run test-gen -d '{"repo_path": ".", "target": "src/agentforge/tools/patching.py"}'

# document a module (prints the documented source; your file is not changed)
uv run agentforge agents run doc-gen -d '{"repo_path": ".", "target": "src/agentforge/tools/patching.py", "readme": false}'

# design an API from a description (prints the spec as YAML)
uv run agentforge agents run api-design -d '{"requirements": "A to-do service: tasks have a title and a done flag; list, create, update and delete them.", "auth": "bearer", "pagination": "offset"}'

# design a PostgreSQL schema (prints DDL, SQLAlchemy models and a Mermaid ER diagram)
uv run agentforge agents run db-schema -d '{"requirements": "A library: books (title, isbn unique), members (name, email unique), and loans linking a member to a book with a due date."}'

# migrate code to Python 3.12 (prints the plan and a diff; your files are not changed)
uv run agentforge agents run migration -d '{"repo_path": ".", "paths": ["src"], "rules": ["utcnow", "unittest-aliases"]}'

# find slow code (add profile_script to measure with cProfile; that script is run)
uv run agentforge agents run performance -d '{"repo_path": ".", "paths": ["src"], "profile_script": "scripts/benchmark.py"}'

# audit code for vulnerabilities (read-only: bandit and a secrets scan, then triage)
uv run agentforge agents run security-audit -d '{"repo_path": ".", "paths": ["src"], "min_severity": "medium"}'

# refactor a module that has tests (prints the diff; your file is not changed)
uv run agentforge agents run refactor -d '{"repo_path": ".", "target": "src/agentforge/tools/patching.py"}'
```

The Code Review agent only trusts what it can check. Findings on files that aren't in the diff are dropped, line numbers outside the changed hunks are cleared, and the verdict always follows from the findings: any `critical` or `major` finding means `request_changes`. It ships with 5 golden eval cases (SQL injection, off-by-one, hardcoded secret, missing `await`, and a clean refactor), which `make test-live` scores against your configured model.

The Bug Fix agent never hands back a patch it hasn't tried. Its patch is applied in memory to the real files (nothing is written to disk); if it doesn't apply, or changes nothing, the model gets the exact mismatch back and one chance to fix it, and `patch_applies` in the output tells you the result. Its 5 golden cases (division by zero, missing defaults, `None` from `re.match`, an off-by-one index and a `str` + `int` concatenation) are scored by applying the patch and running a small behaviour check in a separate Python process.

The Test Generation agent only returns tests that have passed. It runs them with pytest and, on failure, sends pytest's output back to the model, up to `max_attempts` runs (default 3). The output includes the final run (tests, failures, coverage of the target) and the coverage from any existing tests you passed in. Tests run in a temporary directory with a separate `python -I`, a minimal environment (no API keys), a timeout, and CPU, memory and file-size limits (Linux and macOS). That is careful, but it is not a sandbox: only run it on code you trust. Its 5 golden cases are scored on coverage and on **mutants**, small deliberate bugs the tests must catch, so tests that merely call the code do not pass.

The Documentation agent cannot change your code: the model sends docstrings by name, AgentForge places them, and the syntax trees before and after must match apart from docstrings. Every public definition must end up documented, and each new docstring must pass a rubric (every parameter described, every exception the function raises named, a Returns or Yields section where needed, a one-line summary). `>>>` examples are run as doctests. Problems go back to the model up to twice. Its 5 golden cases cover all three styles, classes, generators and a partly documented module.

The API Design agent returns a spec that has been validated, not just written. The model's YAML is checked with the official OpenAPI 3.1 schema (`openapi-spec-validator`, which also resolves every `$ref`), then linted: every operation needs a unique `operationId`, a summary, a success response and an error response; every path parameter is declared; `GET` and `DELETE` have no body; the security scheme you asked for exists and covers every endpoint (public ones say `security: []`); and every list endpoint takes your chosen pagination parameters. Problems go back to the model up to twice, with the exact location. Style findings (verb-like paths, missing tags, unused schemas) come back as `warnings` without blocking. The returned `spec` is canonical YAML. Its 5 golden cases (tasks, bookstore, accounts, documents and orders) check that every required operation, field, status code, enum and media type is in the spec, and that it does not wander beyond what was asked.

The Database Schema agent returns a schema that has been checked, not just written. The DDL goes through PostgreSQL's own parser (`pglast`), and only schema definitions are accepted (`DROP`, `INSERT`, `GRANT` and transactions are refused, so a model-written script can never change data). Then rules: every table has a primary key, every foreign key points at a real, unique, same-typed column in a table created **earlier**, and every type is a known one. The SQLAlchemy models are imported in a separate `python -I` process and must match the DDL table by table: columns, nullability, primary keys and foreign keys. The ER diagram is generated from the checked DDL, so it cannot disagree with it. Problems go back to the model up to twice. Style findings (`timestamp` without time zone, `float` for money, unindexed foreign keys) come back as `warnings`.

For the strongest check, point `AGENTFORGE_CHECK_DATABASE_URL` at a **scratch** PostgreSQL database (and have `psql` installed): the DDL is then also run on that server inside a throw-away schema and rolled back, and the output says `executed: true`. The server's own error message goes back to the model if it rejects the DDL. Without the variable the step is skipped and `executed` is `null`. CI runs these checks against a PostgreSQL 16 service. Its 5 golden cases (blog, shop, school, booking and multi-tenant SaaS) check tables, columns, foreign keys, uniqueness, `CHECK` constraints, types, nullability and enums.

The Security Audit agent never runs the code it reads. Bandit runs in a separate process on a temporary copy, and a small secrets scanner covers `.env`, YAML and JSON files (placeholders such as `changeme` are ignored, and secrets are redacted in the report). Every scanner finding is numbered `S1`, `S2`, and the model must triage **each one**: confirmed (with a concrete fix), false positive (with the reason it is safe) or needs review. A finding the scanner rates high severity with high confidence cannot be dismissed. The model may also report vulnerabilities scanners cannot see, such as path traversal, SSRF or a missing ownership check, but each one needs a quote that really appears within three lines of the cited line, and it may not repeat a scanner finding. Anything that fails goes back to the model up to twice. AgentForge then builds the report itself: merged by file, line and category, sorted by severity, with false positives listed separately and an overall `risk`.

The Performance agent reads source as syntax trees and never runs it, with one exception you control: if you pass `profile_script`, that script runs once under `cProfile` in a separate process (temporary copy, minimal environment, 60 second limit) so the report is based on measured time, not guesses. Functions with 5% or more of the run time are listed; a measured function with no static finding becomes its own candidate (`H1`), and static candidates inside a measured function carry its share. The model must triage **every** candidate (real cost, harmless here, or needs a measurement), and a candidate that took 25% or more of the run time cannot be dismissed. Slowdowns the tools cannot see, such as exponential recursion that needs caching, may be added only with a quote that appears within three lines of the cited line. The final report is merged, sorted by impact and then by measured share, and names the `bottleneck`. It does not rewrite your code; the fixes are suggestions.

The Migration agent starts from a scanner, not from the model's opinion. It reads syntax trees for seven kinds of site: `typing` generics, `Optional`/`Union`, `datetime.utcnow()`, `collections` ABCs, removed `unittest` aliases, removed modules (`imp`, `distutils`, `asyncore` ...) and removed functions (`inspect.getargspec`, `base64.encodestring` ...). The model returns a plan (ordered steps with a risk each) and the complete new content of each changed file. AgentForge then checks it: every file parses and really changed; the scanner finds **no site left** except ones skipped with a reason, and no new kind of site appeared; every public name and signature is unchanged; the plan covers each changed file; and your tests pass on the migrated code. The tests may already fail before, because removed APIs are often why you migrate, so both results are reported. The classic trap is `datetime.utcnow()`: it is naive, so the fix keeps it naive (`datetime.now(UTC).replace(tzinfo=None)`); an "upgrade" to aware datetimes would break comparisons and is caught by the tests. Problems go back to the model up to twice. Your files are never written to: you get the new files, a unified diff and the plan.

The Refactoring agent changes how code is written, never what it does. It measures smells with the syntax tree first (and skips the model when there are none), refuses to touch a module without tests unless you pass `allow_untested`, and runs the tests before the model sees anything. Then it holds the rewrite to four checks: it parses, every public name keeps its signature, the smells actually went down, and the tests still pass. A failed check goes back to the model with the details, up to twice. Its 5 golden cases are scored with the same checks plus hidden tests the model never sees, so a rewrite that quietly changes behaviour fails.

### Evals

```bash
uv run agentforge eval list                    # suites and their cases
uv run agentforge eval run refactor            # one agent, real model, real tokens
uv run agentforge eval run all --concurrency 4 # every suite
make eval                                      # same as `eval run all`
```

`eval run` refuses the fake provider (it cannot solve real cases), writes a JSON report per suite to `.cache/evals/`, prints pass rate, mean score, tokens and cost, and exits 1 when a suite falls below `--min-pass-rate` (default 0.8). A crashed agent or scorer counts as an error for that case, not for the whole run. When run history is on, every case's run is saved too (`source` is `eval`; skip with `--no-history`).

**Baselines** turn a score into a guard. Record a full run against a real model once, commit the files, and every later run is compared with them:

```bash
uv run agentforge eval run all --save-baseline --markdown docs/evals/results.md   # record (real model only)
uv run agentforge eval run code-review                  # now prints "vs baseline ..." and lists changes
uv run agentforge eval compare old.json new.json        # any two baselines or saved reports
```

A baseline is a small JSON file per agent in `docs/evals/baselines/` with each case's status, score, tokens and cost, plus the model and date. A case that gets worse (passed to failed, failed to error, or a score drop above `--tolerance`, default 0.05) is a regression: it is printed and the command exits 1 even if the pass rate is above the minimum. `--case` runs compare only the cases they ran, and `--save-baseline` refuses `--case` and the fake provider, so a baseline is always a full run of a real model. See [docs/evals/README.md](docs/evals/README.md).

See [docs/AGENT_TEMPLATE.md](docs/AGENT_TEMPLATE.md) for how to write a new agent.

## HTTP API

```bash
uv run agentforge serve                      # http://127.0.0.1:8000, interactive docs at /docs
curl localhost:8000/agents                   # every agent: name, version, description, tags
curl localhost:8000/agents/security-audit    # one agent with its input and output JSON Schemas
curl -X POST localhost:8000/agents/echo/run -H 'Content-Type: application/json' -d '{"text": "hi"}'
```

`POST /agents/{name}/run` takes the agent's input as the body (the same JSON you pass to `agentforge agents run -d`) and always answers with the full `RunResult`. The status code says how the run went: `200` succeeded, `422` the input was invalid, `502` the model's answer never passed the agent's checks, `504` timeout, `500` any other failure; `404` for an unknown agent and `503` when the LLM provider is not configured. The `X-Run-Id` header carries the run id.

**Agents run the code you send them** (tests, profile scripts), and that is not a sandbox (it is limited, see [docs/SECURITY.md](docs/SECURITY.md)), so this is not a public service. The defaults are deliberately strict:

- It listens on `127.0.0.1`. Another host (`--host 0.0.0.0`) is refused unless `AGENTFORGE_API__API_KEY` is set; callers then send `Authorization: Bearer <key>` on everything except `GET /health`.
- `repo_path` is refused (`403`): send the code in `files`. To let callers name a folder on the server, set `AGENTFORGE_API__ALLOW_REPO_PATHS=true` **and** `AGENTFORGE_API__REPO_ROOT`; paths must then stay inside that folder.
- Bodies over 5 MB are refused (`413`) and at most 4 runs execute at once (`AGENTFORGE_API__MAX_BODY_BYTES`, `AGENTFORGE_API__MAX_CONCURRENT_RUNS`).

## Run history

Every agent run can be saved, successful or not, with its input, output or error, tokens, cost and timing. It is off until you give it a database, so nothing is written by surprise:

```bash
# .env
AGENTFORGE_STORAGE__URL=sqlite:///.agentforge/runs.db                       # a local file, no server needed
# AGENTFORGE_STORAGE__URL=postgresql://user:password@localhost:5432/agentforge   # or PostgreSQL

uv run agentforge agents run echo -d '{"text": "hi"}'    # saved as a side effect
uv run agentforge runs list --status failed -n 10        # newest first; also --agent NAME, --json
uv run agentforge runs show <run_id>                     # input, output or error, usage, timing
uv run agentforge runs stats --days 7                    # per agent: runs, success rate, avg time, tokens, cost
uv run agentforge runs prune --days 30                   # delete older runs
```

`agentforge serve` saves every API run too and adds `GET /runs` (`?agent=&status=&limit=&offset=`), `GET /runs/stats` (`?agent=&days=`) and `GET /runs/{run_id}`; these answer `503` when no database is configured and need the API key like everything else. The tables are created on first use; there are no migrations yet.

- **A run never fails because its history could not be saved.** The error is logged (`runs.save_failed`) and the run returns normally. A database URL that cannot be opened stops `agentforge serve` at startup, and is only a warning for `agents run`.
- **Inputs can be sensitive** (source code, diffs, configs). Set `AGENTFORGE_STORAGE__STORE_INPUTS=false` to keep everything but the input, and use `runs prune` to expire old rows. `config show` hides the password in the URL.
- PostgreSQL stores inputs and outputs as `JSONB`. The bundled `psycopg` driver is used for `postgresql://` URLs.

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
├── api/             # HTTP API (FastAPI): list, describe and run agents
├── cli/             # `agentforge` command (Typer + Rich)
├── storage/         # run history in SQLite or PostgreSQL (SQLAlchemy)
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
| `AGENTFORGE_API__HOST` / `__PORT` | `127.0.0.1` / `8000` | Where `agentforge serve` listens |
| `AGENTFORGE_API__API_KEY` | — | Bearer token for the API; required to listen on a non-loopback host |
| `AGENTFORGE_API__ALLOW_REPO_PATHS` / `__REPO_ROOT` | `false` / — | Let callers use `repo_path`, only inside the root folder |
| `AGENTFORGE_API__MAX_CONCURRENT_RUNS` / `__MAX_BODY_BYTES` | `4` / `5000000` | Limits on simultaneous runs and request size |
| `AGENTFORGE_SANDBOX__ENABLED` | `true` | CPU, memory and file-size limits for code the agents run (Linux/macOS); see [docs/SECURITY.md](docs/SECURITY.md) |
| `AGENTFORGE_SANDBOX__CPU_S` / `__MEMORY_MB` / `__FILE_MB` / `__OUTPUT_MB` | `120` / `2048` / `100` / `8` | The limits themselves |
| `AGENTFORGE_RESEARCH__SEARCH_API_KEY` | — (search off) | Brave Search API token for the Research agent's `search`; fetching given `urls` needs no key |
| `AGENTFORGE_RESEARCH__ALLOWED_HOSTS` / `__SEARCH_RESULTS` / `__MAX_PAGE_BYTES` | any public host / `8` / `300000` | Limit which hosts pages may be fetched from, results per search, and page size |
| `AGENTFORGE_STORAGE__URL` | — (history off) | `sqlite:///path.db` or `postgresql://user:password@host/db`; every run is saved there |
| `AGENTFORGE_STORAGE__STORE_INPUTS` | `true` | Keep each run's input next to its output |
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
- [Changelog](CHANGELOG.md) and [release checklist](docs/RELEASE.md)
- [Security notes](docs/SECURITY.md): what runs model-written code and how it is limited
- [Architecture decision records](docs/adr/)

## Roadmap

| Milestone | Target | Scope |
|---|---|---|
| M1 — Portfolio MVP (v0.1.0) | 30 Oct 2026 | Core runtime, CLI, 10 engineering agents, evals, basic API |
| M2 — Serious AI project (v0.2.0) | 27 Nov 2026 | Research, data, RAG, business and delivery agents (24 total) |
| M3 — Flagship (v1.0.0) | 01 Jan 2027 | Orchestrator, memory, permissions, observability, dashboard, docs |
