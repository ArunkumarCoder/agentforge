# Changelog

All notable changes to AgentForge are listed here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project aims to follow [Semantic Versioning](https://semver.org/) from v0.1.0.

## [Unreleased]

Everything below is what v0.1.0 (target: 30 October 2026) will contain. The version number in the code is still `0.0.1` until the release commit.

### Added

**Foundation (weeks 1 and 2)**

- Typed configuration (`AGENTFORGE_*`, `.env` and profile files), structured logging with secret redaction, and the `agentforge` command line (Typer and Rich).
- `BaseAgent` and `LLMAgent`: validated input and output, a repair loop driven by each agent's own `check`, timeouts, and a `RunResult` that never raises.
- A provider-neutral LLM layer: Anthropic, OpenAI, Ollama and a scripted fake provider, with retries, token and cost accounting, and a response cache (memory, disk, replay).
- Typed tools with permissions, a tool-calling loop, built-in workspace, git, allow-listed command and HTTP tools, and the agent registry.

**Ten engineering agents (weeks 3 and 4)**, each checked against reality rather than trusted:

- Code Review (findings must point at changed lines), Bug Fix (patches are applied and tests run), Test Generation (tests are run, coverage measured, mutants must be caught), Documentation (syntax tree must match; doctests run), Refactoring (public API unchanged, tests pass).
- API Design (OpenAPI 3.1 validated and linted), Database Schema (DDL parsed by PostgreSQL's own parser, SQLAlchemy models must match), Security Audit (Bandit and a secrets scanner triaged item by item; claims must quote real code), Performance (static detectors and cProfile hotspots, each fixed or dismissed), Migration (Python 3.12 upgrade sites fixed with the API and tests intact).
- 50 golden eval cases with deterministic scoring (no LLM judge) and `agentforge eval run|list`.

**Platform (weeks 4 and 5)**

- HTTP API (FastAPI): `agentforge serve`, `GET /agents`, `POST /agents/{name}/run`, API key, `repo_path` guard, body and concurrency limits.
- Run history in SQLite or PostgreSQL: `agentforge runs list|show|stats|prune` and `GET /runs`.
- Eval baselines and regression checks: `eval run --save-baseline`, `eval compare`, a Markdown results summary, and eval runs saved to the run history.
- Sandbox limits for model-written code (CPU, memory, file size, output cap, process-group kill) and `docs/SECURITY.md`.
- `agentforge agents example` and `agents run --example`: a ready-made, tested input for every agent, taken from its golden eval cases.
- `agentforge doctor`: one command that checks the runtime, model, price table, database, limits, API exposure, tools, agents and baselines, with an optional live `--ping`.

### Known limitations

- No agent has been scored against a real model yet, so there are no recorded baselines. Recording them is the first job of release week.
- The sandbox limits are a safety net, not isolation; see `docs/SECURITY.md`. They are not applied on Windows.
- Run history has no migrations yet, and tool-call traces are not stored.
- The agents are Python-centric apart from API Design and Database Schema.
