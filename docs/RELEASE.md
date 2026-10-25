# Release checklist

For v0.1.0 (target 30 October 2026) and every release after it. Do the steps in order; each one has a way to check that it worked.

## 1. Record the baselines (needs a real model)

```bash
# .env: AGENTFORGE_LLM__PROVIDER, AGENTFORGE_LLM__MODEL and the API key
uv run agentforge doctor --ping           # no failures; the model must be priced (no "$0.00" warning)
uv run agentforge eval run code-review    # one agent first: see the cost and the time
uv run agentforge eval run all --concurrency 4 --save-baseline --markdown docs/evals/results.md
```

- Read `docs/evals/results.md`. Any case that fails or errors is either a prompt problem, a scorer problem or a model limit. Fix the cause, or write it down as a known limitation, never loosen a check to make a number look better.
- Run `eval run all` a second time: no regressions against the baseline you just recorded. A case that flips on identical code is flaky and needs fixing.
- Commit `docs/evals/` with the prompt or scorer changes that produced it.

## 2. Checks

```bash
make check                                  # lint, mypy, tests, coverage >= 80%
uv run mypy --platform win32                # the Windows type check
uv run pre-commit run --all-files
uv lock --check
```

Also confirm CI is green on Linux and Windows for the release commit.

## 3. Version and notes

- Set the version in `pyproject.toml` and `src/agentforge/__init__.py` (they must match; a test and `make package` both check it), then run `uv lock` so `uv.lock` carries the same version.
- In `CHANGELOG.md`, rename `[Unreleased]` to the version and date, add a fresh empty `[Unreleased]` above it, and replace "no baselines" under known limitations with the recorded pass rate and the model.
- Update the README status line.

## 4. Build and test the package on its own

```bash
make package          # uv run python scripts/check_package.py
```

It builds the wheel and sdist, checks that every prompt and eval file is inside the wheel and that no `.env`, `docs/prompts`, cache or coverage file is in either, installs the wheel into a fresh virtual environment and, from an empty folder with no AgentForge settings or API keys in the environment, runs `--version`, `agents list`, `eval list`, `agents run echo --example`, `agents example`, `config show` and `doctor`. It exits 1 and lists the problems if anything is wrong. The same check runs in CI as the `package` job. Add `--keep` to leave the built files and the venv in place for a look.

## 5. Tag and publish

```bash
git tag -a v0.1.0 -m "AgentForge v0.1.0"
git push origin main --tags
```

Create the GitHub release from the tag with the changelog section as the notes. Attach the wheel and sdist from `dist/`. State plainly in the notes that `agentforge serve` is a private service and the sandbox is a safety net (see `docs/SECURITY.md`).

## 6. After the release

Start `[Unreleased]` in the changelog, and open the milestone 2 issues.
