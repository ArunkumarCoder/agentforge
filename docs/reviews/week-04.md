# Week 4 review

**Dates:** 12 to 19 October 2026. **Goal:** the other five engineering agents, then one way to reach all of them over HTTP.

## What shipped

| Day | Agent or feature | What it proves instead of trusts |
|---|---|---|
| 16 | API Design | The OpenAPI 3.1 spec is validated by `openapi-spec-validator` (including every `$ref`) and linted: operationIds, error responses, auth, pagination |
| 17 | Database Schema | The DDL is parsed by PostgreSQL's own parser (`pglast`) and linted; the SQLAlchemy models must match it table by table |
| 18 | Security Audit | Bandit and a secrets scanner produce numbered findings; the model must triage each one; every extra vulnerability must quote real code within three lines |
| 19 | Performance | Static slow-pattern detectors plus a cProfile run produce numbered candidates and hotspots; the model must fix or dismiss each, and the hottest paths must be kept |
| 20 | Migration | A rule scanner finds Python 3.12 upgrade sites; every site must be fixed or skipped with a reason, the public API must not change and the tests must stay green |
| 21 | HTTP API | `agentforge serve` exposes `/agents` and `/agents/{name}/run`; the response is always the full `RunResult` and the status code follows its error kind |

With Migration there are ten agents, so the engineering set is complete: Code Review, Bug Fix, Test Generation, Documentation, Refactoring, API Design, Database Schema, Security Audit, Performance and Migration.

## What worked

- **The candidate pattern.** Days 18 to 20 share one shape: a static tool numbers the candidates (`S1`, `P1`, `M1`), the model must answer for every one, `check` verifies that each id is covered exactly once, and `postprocess` builds the final report itself. Each new agent was mostly its scanner plus a definition of "covered".
- **Scoring without a judge.** Every agent has `scoring.py` and five golden cases. The tests use reference answers that must score full marks, so a case with a wrong expectation fails offline instead of surviving until a live run.
- **Shared tools again.** `python_ast`, `pytest_runner` and `Workspace` were reused by Performance and Migration. Only the scanners (`security_scan`, `perf_scan`, `py_migrate`) were new.
- **The API stayed thin.** It adds no agent logic. `RunResult` never raises, so the HTTP layer is a status mapping plus guards, and every status code has a test.

## What did not

- **Scanner counts were guessed.** On Day 18 the expected counts in a case were wrong and a ±1 line match matched a neighbouring safe line. Expected values are now measured from the scanner and scanner findings match on the exact line.
- **Detector false positives.** Day 19's string-concat detector treated `s = s + x` as non-string and compared `Load` against `Store` nodes, and the profiler picked up `<frozen ...>` frames. Day 20 matched `from collections.abc import Mapping` by its first module component. Each was found by a test that planted the case; none by luck.
- **Flaky VM reads, still.** A few eval JSON files raise `PermissionError` through the device VM (`doc_gen`, `refactor`, `performance`), which fails `tests/cli/test_cli.py::TestEval` there. It does not happen in the cloud copy or in CI and is not an AgentForge bug.
- **Still no real-model numbers.** `make test-live` and `make eval` have not been run against a live provider for any of the ten agents. This was a risk last week and it is now the largest one.

## Numbers

- 10 agents, 50 golden cases (5 each), 847 offline tests, coverage about 97.7%, mypy strict clean across 105 source files.
- New runtime dependencies this week: `pglast`, `sqlalchemy`, `openapi-spec-validator`, `pyyaml`, `bandit`, `fastapi` and `uvicorn`. `httpx` was added as a dev dependency for API tests.
- Week 4 added about 12,000 lines across 113 files, roughly 40% of it tests and golden cases.

## Risks to carry into week 5

1. **No measured pass rates.** Run `make eval` against a real model first thing and record per-agent scores and cost in `docs/`. Everything in the prompts is tuned against the fake provider so far.
2. **The API runs the code it is given.** Agents execute and read supplied code (`pytest_runner`, `profile()`, `repo_path`). It is not a sandbox. It binds to loopback by default, requires an API key on any other host and confines `repo_path` to `repo_root`, but it must stay private until week 5 hardening decides on real isolation.
3. **Runs are not persisted.** `RunResult` lives in the response only. PostgreSQL run storage is the next piece and the API's `X-Run-Id` header is already the key for it.
4. **Eval JSON read quirk on the device VM.** Harmless, but it hides real failures behind noise; consider running the suite in CI only for those cases.

## Next week

Hardening (timeouts, limits, error messages, a sandbox decision), PostgreSQL and pgvector run persistence, eval harness v1 with recorded baselines, and the v0.1.0 release on 30 October 2026.
