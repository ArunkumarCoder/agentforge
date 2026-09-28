# ADR-0001: Core tech stack

- **Status:** Accepted
- **Date:** 2026-09-28
- **Deciders:** Arunkumar

## Context

AgentForge is a 14-week, solo, portfolio-grade project: 25 LLM agents on a shared runtime, a multi-agent orchestrator, RAG, evaluations, an API and a dashboard. The stack must:

- have first-class support for LLM SDKs, data work and evaluation tooling;
- give strong typing so agent outputs can be validated and chained;
- run tests offline and cheaply in CI;
- be recognisable to reviewers and employers looking at the repository.

## Decision

| Area | Choice |
|---|---|
| Language | Python 3.12 |
| Packaging / env | uv (lockfile, `uv run`), src/ layout, hatchling build backend |
| Lint / format / types | ruff, mypy (strict), pre-commit |
| Schemas & config | Pydantic v2, pydantic-settings |
| LLM access | Own `LLMProvider` interface over the official Anthropic / OpenAI SDKs, plus Ollama and a Fake provider |
| Agent runtime | Own `BaseAgent` and tool-calling loop (see ADR-0002, week 2) |
| Orchestration | LangGraph, for Agent 25 only |
| Storage | PostgreSQL 16 + pgvector, SQLAlchemy 2 (async), Alembic |
| Interfaces | Typer + Rich (CLI), FastAPI (API, SSE), Next.js (dashboard) |
| Testing | pytest, pytest-asyncio, pytest-recording, testcontainers, Playwright |
| Observability | structlog, OpenTelemetry, Langfuse or Phoenix (decided in week 12) |
| Delivery | Docker, docker-compose, GitHub Actions, GHCR |

## Alternatives considered

- **TypeScript / Node for the backend:** strong typing and matches the Next.js frontend, but the Python ecosystem for RAG, data analysis, sandboxed pandas and evaluation is much deeper. Rejected.
- **LangChain agents for everything:** faster to start, but it hides the runtime concepts this project is meant to demonstrate, and upgrades often break things. We use LangGraph only where graph orchestration adds real value.
- **Poetry / pip-tools instead of uv:** both work; uv is faster and handles Python versions, the virtualenv and the lockfile in one tool.
- **A dedicated vector DB (Qdrant, Weaviate):** one more service to run. pgvector keeps runs, memory, checkpoints and embeddings in a single Postgres.

## Consequences

- **Positive:** one language for runtime, evals and data work; typed contracts between agents; a single database; CI without API keys.
- **Negative:** the own runtime costs about 2 weeks up front (weeks 1–2); the Next.js dashboard adds a second language late in the project (week 13).
- **Follow-ups:** ADR-0002 (own runtime vs. framework), ADR-0003 (orchestration design, week 10).
