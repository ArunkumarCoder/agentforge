# Week 5 review

**Dates:** 19 to 23 October 2026. **Goal:** turn ten working agents into something that can be run, kept track of and trusted: history, evals that guard, limits on the code they run, and a way to check an installation.

## What shipped

| Day | Piece | What it gives you |
|---|---|---|
| 22 | Week 4 review | The numbers and risks of the first four weeks |
| 23 | Run history | Every run (input, output or error, tokens, cost, timing) saved to SQLite or PostgreSQL; `agentforge runs list\|show\|stats\|prune`; `GET /runs`, `/runs/stats`, `/runs/{id}` |
| 24 | Eval harness v1 | Committed baselines, `eval compare`, regression checks that exit 1, a Markdown results summary, eval runs saved to the history |
| 25 | Sandbox hardening | Process-group kill on timeout, an output cap, CPU, memory and file-size limits for model-written code, `docs/SECURITY.md` |
| 26 | `agentforge doctor` | One command that checks the runtime, model and price, database, limits, API exposure, tools, agents and baselines; `CHANGELOG.md` |
| 27 | This review | Two ADRs, a release checklist, and a check that the built package works on its own |

## What worked

- **Opt-in persistence, safe failure.** History is off until a URL is set, and a database error never fails a run (`try_save` logs and returns). The same store served the CLI, the API and the eval runner without any of them knowing about SQL.
- **One choke point for running programs.** Every place that runs model-written code already went through `run_process`, so the hardening (group kill, output cap, limits) was one function plus four call sites, and the limit tests could be written once.
- **A launcher instead of `preexec_fn`.** Setting resource limits in a tiny `python -I -c` launcher that then `exec`s the real program avoids the thread-safety problems of `preexec_fn` in a process that uses threads for the API and the run store.
- **Checks that name the cause.** `doctor` found, by design, the problems that would otherwise surface half way through a paid run: no key, an unpriced model (every cost would read $0.00), an unreachable database, no baselines.
- **The package stands on its own.** `uv build` produces a wheel with all 10 prompts and all 50 eval cases and no stray files (no `.env`, no `docs/prompts`). Installed into a clean Python 3.12 environment and run from an empty folder, `agentforge --version`, `agents list`, `eval list`, `agents run echo` and `doctor` all work.

## What did not

- **A Windows-only type error reached the repository.** `mypy` on Windows treats `sys.platform == "win32"` as always true, so code after it was unreachable and CI-style checks failed on the developer's machine but not in Linux. It was fixed on Day 26; from now on `mypy --platform win32` is part of the check.
- **The commit hook cannot run everywhere.** The repository's hook is a Windows script, so a commit made from a Linux shell has to run the checks by hand. Not a bug, but worth knowing when automating commits.
- **Baselines are still empty.** Everything needed to record them exists and is tested, but no agent has been scored against a real model. This was the top risk last week and it is still the top risk.
- **Limits are partial.** No process-count limit (it would break normal use), no limits on Windows, and the macOS kernel ignores the address-space limit. `docs/SECURITY.md` says so, and ADR-0004 records why a container was not chosen.

## Numbers

- 10 agents plus `echo`, 50 golden cases, 977 offline tests (2 skipped for lack of a real PostgreSQL), coverage about 97.7%, mypy strict clean across 111 source files on both Linux and Windows settings.
- One new runtime dependency this week: `psycopg[binary]` (PostgreSQL driver). `httpx` stays a dev dependency.
- Week 5 added about 3,400 lines (1,700 in `src`, 1,700 in tests), so the tests are about half of it.
- Built package: a 269 KB wheel and a 431 KB source distribution.

## Risks to carry into week 6

1. **No real-model numbers.** Record the baselines first (`docs/RELEASE.md`, step 1). If a prompt is far off, fix the prompt, not the check, and record the new baseline in the same commit.
2. **Cost is unknown.** Fifty cases across ten agents will cost real money; `doctor` confirms the model is priced and `eval run --concurrency 4` keeps it fast, but the first number is a surprise until it is measured. Run one agent first.
3. **Pass rates may be uneven.** Some agents (Test Generation, Refactoring) repair several times; a flaky case should have its check tightened, not its tolerance widened.
4. **The sandbox is a safety net, not a wall.** Do not advertise `agentforge serve` as safe for strangers in the release notes.
5. **CI on Windows has not seen the Week 5 tests yet.** The POSIX-only limit tests skip there; if CI fails, look at the timing-based tests first.

## Next week

Release week: record the baselines against a real model and commit them, fix whatever they show, bump the version and tag v0.1.0 on 30 October 2026 (see `docs/RELEASE.md`), then start milestone 2 (research, data and RAG agents) with the shared pgvector store.
