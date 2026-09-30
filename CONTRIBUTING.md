# Contributing to AgentForge

## Setup

Requirements: [uv](https://docs.astral.sh/uv/) and git. uv installs Python 3.12 for you if needed.

```bash
make install   # or: uv sync
make hooks     # or: uv run pre-commit install
make check     # lint + types + tests; must pass before every commit
```

No `make` (e.g. on Windows)? Run the commands shown in the `Makefile` directly, e.g. `uv run pytest`.

## Everyday commands

| Command | What it does |
|---|---|
| `make fmt` | Format and auto-fix lint issues |
| `make lint` | Check formatting and lint without changing files |
| `make type` | mypy in strict mode |
| `make test` | Run tests |
| `make cov` | Tests with coverage (fails under 80%) |
| `make check` | Everything CI runs |

## Project conventions

- **Layout:** code in `src/agentforge/`, tests in `tests/` mirroring the package structure.
- **Types:** everything is type-annotated; mypy runs in strict mode.
- **Dependency rule:** a layer imports only from the layers below it (see `docs/ARCHITECTURE.md` §4).
- **Tests run offline.** Never call a real LLM in the default test run; mark such tests with `@pytest.mark.llm`.
- **Decisions:** anything hard to reverse gets an ADR in `docs/adr/` (copy `0000-template.md`).

## Commit messages

We use [Conventional Commits](https://www.conventionalcommits.org/):

```
<type>(<optional scope>): <short summary in imperative mood>

<optional body: what and why, wrapped at 72 characters>
```

Types: `feat`, `fix`, `docs`, `test`, `refactor`, `chore`, `ci`, `build`, `perf`.

Examples:

```
feat(llm): add Anthropic provider with retry and timeout
chore: configure ruff, mypy and pre-commit
docs(adr): record ADR-0002 own agent runtime
```

## Branches

- `main` is always green.
- Work on short-lived branches: `feat/llm-provider`, `docs/architecture`.
- Tag releases as `vX.Y.Z` at the end of each milestone.
