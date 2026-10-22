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

- Set the version in `pyproject.toml` and `src/agentforge/__init__.py` (they must match: `agentforge --version`).
- In `CHANGELOG.md`, rename `[Unreleased]` to the version and date, add a fresh empty `[Unreleased]` above it, and replace "no baselines" under known limitations with the recorded pass rate and the model.
- Update the README status line.

## 4. Build and test the package on its own

```bash
rm -rf dist && uv build
uv venv --python 3.12 /tmp/release-check && uv pip install --python /tmp/release-check/bin/python dist/*.whl
cd "$(mktemp -d)" && /tmp/release-check/bin/agentforge --version && /tmp/release-check/bin/agentforge doctor
```

The wheel must contain the 10 `prompt.md` files and the 50 eval JSON files, and no `.env` or `docs/prompts`.

## 5. Tag and publish

```bash
git tag -a v0.1.0 -m "AgentForge v0.1.0"
git push origin main --tags
```

Create the GitHub release from the tag with the changelog section as the notes. Attach the wheel and sdist from `dist/`. State plainly in the notes that `agentforge serve` is a private service and the sandbox is a safety net (see `docs/SECURITY.md`).

## 6. After the release

Start `[Unreleased]` in the changelog, and open the milestone 2 issues.
