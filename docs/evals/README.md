# Eval baselines

Every agent has five golden cases. A baseline records how one full run against a real model went, so later changes (a new prompt, a new model, a refactor) can be judged against it.

## Record the first baseline

This costs real tokens (one agent run per case, 50 cases for all ten agents).

```bash
# .env: AGENTFORGE_LLM__PROVIDER, AGENTFORGE_LLM__MODEL and the API key
uv run agentforge eval run all --concurrency 4 --save-baseline --markdown docs/evals/results.md
git add docs/evals
```

This writes `docs/evals/baselines/<agent>.json` (one file per agent, stable keys, so the git diff shows exactly which cases changed) and a readable `docs/evals/results.md`.

## Check a change against it

```bash
uv run agentforge eval run code-review          # one agent
uv run agentforge eval run all                  # everything (make eval)
```

Under each agent's table the output shows a line such as

```text
vs baseline (anthropic/claude-sonnet, 2026-10-23) on 5 case(s): pass rate 80% → 60% · ...
  regression off_by_one: passed 1.00 → failed 0.50
```

and the command exits 1 on any regression.

## What counts as a regression

For each case present in both runs, comparing status and score:

| Before | After | Result |
|---|---|---|
| passed | failed or error | regression |
| failed | error | regression |
| failed | passed | improvement |
| same status | score down by more than `--tolerance` (default 0.05) | regression |
| same status | score up by more than the tolerance | improvement |

Cases only in the new run are listed as new; cases only in the baseline are listed as not run (a `--case` run is a subset, so that is never a failure). Totals use only the cases both runs have.

## Rules

- A baseline always comes from a **full run of a real model**. `--save-baseline` refuses `--case` and the fake provider.
- Re-record a baseline on purpose (a better prompt, a new model), review the diff, and commit it with the change that caused it.
- Model output varies from run to run. If one case flips between passed and failed on identical code, widen its check or fix the prompt; do not raise `--tolerance` to hide it.
- Compare any two files, including the reports `eval run` saves to `.cache/evals/`:

```bash
uv run agentforge eval compare docs/evals/baselines/code-review.json .cache/evals/<run>-code-review.json
```
