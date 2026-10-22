# ADR-0005: Run history in SQLAlchemy Core, SQLite by default, PostgreSQL supported

- **Status:** Accepted
- **Date:** 2026-10-19
- **Deciders:** Arunkumar

## Context

Cost, failure rates and regressions need history: each run's input, output or error, tokens, cost and timing. The roadmap names PostgreSQL (and later pgvector for memory and RAG), but a command-line tool that needs a database server before it can show a result is a poor first experience.

Forces:

- **Zero-setup default.** A local file should be enough to try `agentforge runs stats`.
- **One production database.** PostgreSQL is where memory and RAG will live, so the same store should work there.
- **Runs must never fail because of history.**
- **No schema churn yet.** The model is one table; migrations would be ceremony.

## Decision

1. `RunStore` uses SQLAlchemy **Core** with one `runs` table. `JSON` columns become `JSONB` on PostgreSQL. SQLite and PostgreSQL (through the bundled `psycopg` driver) are the only supported backends.
2. History is **opt-in**: nothing is written until `AGENTFORGE_STORAGE__URL` is set.
3. Saving goes through `try_save`, which logs `runs.save_failed` and returns `False` on any error. A bad URL stops `agentforge serve` at startup (fail fast) but is only a warning for `agents run`.
4. Inputs are stored by default (`STORE_INPUTS=false` turns that off), because a run cannot be reproduced without them; `runs prune` expires old rows.
5. Tables are created on first use. The first schema change introduces Alembic.

## Alternatives considered

- **ORM models:** more code for one table, and typing the declarative layer under strict mypy costs more than it gives.
- **PostgreSQL only:** simplest to reason about, but it makes a server a prerequisite for the first useful command.
- **JSON-lines files:** trivial, but no querying, paging or stats without writing a query engine.
- **Alembic now:** the right tool once the schema changes; premature for one table that is created if missing.

## Consequences

- **Positive:** one store serves the CLI, the API and the eval runner. Behaviour is tested on SQLite and, when a scratch URL is given, on a real PostgreSQL.
- **Negative:** no migrations yet, so a column change needs a manual step or a new table until Alembic lands. Stored inputs may contain source code and need the same care as the code itself.
- **Follow-ups:** Alembic with the first schema change; tool-call traces; a pgvector table for the memory and RAG agents in milestone 2.
