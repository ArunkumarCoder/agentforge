# Week 3 review

**Dates:** 5 to 9 October 2026. **Goal:** the first five agents, each checked against reality, plus a way to score them.

## What shipped

| Day | Agent | What it proves instead of trusts |
|---|---|---|
| 11 | Code Review | Findings must point at files and lines that are in the diff; the verdict follows from the findings |
| 12 | Bug Fix | The patch is applied in memory to the real files; a patch that does not apply is sent back |
| 13 | Test Generation | The tests are run with pytest; coverage is measured; mutants must be caught |
| 14 | Documentation | The syntax tree must match apart from docstrings; a rubric and doctests check the text |
| 15 | Refactoring | Public API unchanged, smells reduced, tests pass before and after; hidden tests in evals |
| 15 | Eval runner | `agentforge eval run <agent|all>` scores every golden case and saves a JSON report |

## What worked

- **One idea, five times.** Every agent has a deterministic check that does not ask a model whether the model was right. That made each agent testable offline with the fake provider and made the golden cases scoreable without an LLM judge.
- **Repair loop in the base class.** `LLMAgent.check` raises `OutputCheckError` and the failure goes back into the transcript. Agents 2 to 5 reuse it, so each only had to say what "correct" means.
- **Shared tools.** `patching`, `pytest_runner` and `python_ast` were each written for one agent and reused by the next. Day 15's refactor agent needed three new helpers (`public_api`, `api_changes`, `shadow_files`), not a new subsystem.
- **Shared scoring types.** `Check` and `CaseScore` now live in `agentforge.evals`, so the runner can score any agent the same way.

## What did not

- **Hidden-test arithmetic.** Two golden cases had wrong expected values on first write. Reference solutions must be run against the hidden tests before a case is trusted; the generator script now does this.
- **Flaky VM reads.** Some eval JSON files could not be read through the device VM; hashes were compared from the cloud copy instead. Not an AgentForge bug, but it cost time on three days.
- **Costs are unmeasured.** The live tests (`make test-live`) have not been run against a real model for all five agents together. The first `make eval` run is the baseline and should be recorded in week 5.

## Numbers

- 5 agents, 25 golden cases (5 each), about 570 offline tests, coverage 97%, mypy strict clean across 71 source files.
- No new runtime dependencies this week beyond Day 3's.

## Risks to carry into week 4

1. Pass rates are untested on a real model. Do one `make eval` run early in week 4 and write down the numbers.
2. `pytest_runner` isolates with `python -I`, a minimal environment and a timeout, but it is not a sandbox. Fine for code you own; do not expose it to strangers before week 5.
3. The agents are Python-only. API Design and Database Schema (week 4) are language-neutral, so they are a good test of whether the repair loop generalises.

## Next week

Agents 6 to 10: API Design (OpenAPI that validates), Database Schema (DDL that executes), Security Audit (seeded vulnerabilities found), Performance (known hotspots found) and Migration, plus a basic FastAPI service around the registry.
