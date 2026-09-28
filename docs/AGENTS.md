# Agent Catalog

All 25 agents, grouped by category. The week column is when each is built. Every agent must meet the Definition of Done below before it's marked done.

## Code quality

| # | Agent | Week | Purpose | Key tools | Validated by |
|---|---|---|---|---|---|
| 1 | Code Review | 3 | Reviews a diff and returns findings with severity, location and fix suggestion. | git_diff, file_read, ruff | Golden diffs with known issues |
| 2 | Bug Fix / Debug | 3 | Stack trace + code → root cause and a minimal patch. | file_read, test_runner | Patch applies and failing test passes |
| 3 | Test Generation | 3 | Generates pytest tests for a module and self-verifies by running them. | file_read, pytest, coverage | Tests pass; coverage increases |
| 4 | Documentation | 3 | Writes docstrings, module READMEs and usage examples. | file_read, AST parser | Docstring coverage; judge rubric |
| 5 | Refactoring | 3 | Detects code smells and refactors while keeping tests green. | file_read, test_runner, ruff | Tests still pass; complexity drops |

## Architecture

| # | Agent | Week | Purpose | Key tools | Validated by |
|---|---|---|---|---|---|
| 6 | API Design | 4 | Requirements → OpenAPI 3.1 spec with schemas and errors. | openapi-spec-validator | Spec validates |
| 7 | Database Schema | 4 | Entities → ERD, DDL and SQLAlchemy models. | SQL executor (test DB) | DDL executes on Postgres |
| 8 | Security Audit | 4 | Static scan + LLM triage into prioritized, deduplicated findings. | bandit, semgrep | Seeded vulnerabilities found |
| 9 | Performance | 4 | Analyzes profiles/code for hotspots and complexity issues. | cProfile reader, radon | Known hotspots identified |
| 10 | Migration | 4 | Plans and drafts framework/version migrations. | file_read, dependency parser | Judge rubric + compiles |

## Knowledge

| # | Agent | Week | Purpose | Key tools | Validated by |
|---|---|---|---|---|---|
| 11 | Research | 6 | Multi-step web research with verified inline citations. | web_search, http_fetch | All citations were fetched |
| 12 | Data Analysis | 6 | Runs pandas in a sandbox; returns findings and charts. | python_sandbox | Numeric answers match |
| 13 | Document Extraction | 6 | PDF/DOCX → schema-validated JSON with confidence. | PyMuPDF, python-docx | Field-level accuracy |
| 14 | RAG Knowledge | 7 | Answers from an indexed corpus with citations; refuses when unsupported. | pgvector, hybrid search, reranker | hit@k, MRR, faithfulness |
| 15 | Prompt Optimizer | 7 | Generates prompt variants and selects the best against an eval set. | eval runner | Measured score lift |

## Business

| # | Agent | Week | Purpose | Key tools | Validated by |
|---|---|---|---|---|---|
| 16 | Business Analyst | 8 | Brief → user stories with Gherkin acceptance criteria. | — | Schema + judge rubric |
| 17 | Product Manager | 8 | Stories → PRD, RICE prioritization, roadmap. | — | Contract test with BA output |
| 18 | Project Planner | 8 | PRD → WBS, estimates, sprints and dependencies. | — | Valid DAG; contract test |
| 19 | Meeting & Communication | 8 | Notes/transcripts → decisions, action items, follow-up email. | — | Action-item recall |
| 20 | Content Writer | 8 | Brief + style guide → blog/LinkedIn/marketing copy. | — | Style rubric |

## Delivery

| # | Agent | Week | Purpose | Key tools | Validated by |
|---|---|---|---|---|---|
| 21 | GitHub | 9 | PR summaries, issue triage and labels (dry-run by default). | GitHub API | Recorded API tests |
| 22 | DevOps | 9 | Generates Dockerfiles, compose files and CI workflows. | hadolint, actionlint | Linters pass |
| 23 | Log & Incident Analysis | 9 | Clusters errors in logs and writes an RCA report. | log parser | Root cause identified |
| 24 | QA | 9 | Acceptance criteria → test plan, cases, Playwright skeletons. | Playwright | Coverage of criteria |

## Orchestration

| # | Agent | Week | Purpose | Key tools | Validated by |
|---|---|---|---|---|---|
| 25 | Orchestrator | 10 | Routes, plans and runs multi-agent workflows with HITL, memory and permissions. | LangGraph, all agents | Routing accuracy, E2E success |

## Definition of Done (per agent)

- [ ] Input and output are Pydantic models; output is validated, with one automatic retry when invalid
- [ ] Prompt lives in `prompt.md` (versioned), not inline in code
- [ ] Tools declared with permission levels (`read` / `write` / `network` / `exec`)
- [ ] Agent card: name, description, capabilities, input/output schema
- [ ] 5–10 golden eval cases (20+ for RAG and Prompt Optimizer, 30 for the Orchestrator) with recorded pass rate
- [ ] Unit tests run offline (FakeLLM or recorded responses)
- [ ] Runnable from CLI and API; example listed in README
- [ ] Logs tokens, cost and latency for every run
