.DEFAULT_GOAL := help
.PHONY: help install hooks fmt lint type test test-live eval cov package check clean

help:  ## Show available commands
	@grep -E '^[a-z-]+:.*## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*## "}; {printf "  \033[36m%-10s\033[0m %s\n", $$1, $$2}'

install:  ## Create .venv and install all dependencies
	uv sync

hooks:  ## Install git pre-commit hooks
	uv run pre-commit install

fmt:  ## Format code (ruff format + auto-fixable lint)
	uv run ruff format .
	uv run ruff check --fix .

lint:  ## Lint without changing files
	uv run ruff format --check .
	uv run ruff check .

type:  ## Type-check with mypy (strict), for Linux and for Windows
	uv run mypy
	uv run mypy --platform win32

test:  ## Run tests
	uv run pytest

test-live:  ## Run live LLM smoke tests (needs API keys; costs a few cents at most)
	uv run pytest -m llm -v

eval:  ## Run every agent's golden eval cases against the configured model (costs tokens)
	uv run agentforge eval run all

cov:  ## Run tests with coverage report (fails under 80%)
	uv run pytest --cov --cov-report=term-missing --cov-report=html

package:  ## Build the wheel and sdist, check their contents, install and run the wheel
	uv run python scripts/check_package.py

check: lint type cov  ## Everything CI runs: lint + types + tests with coverage

clean:  ## Remove caches and build output
	rm -rf .pytest_cache .mypy_cache .ruff_cache htmlcov .coverage dist build
	find . -type d -name __pycache__ -prune -exec rm -rf {} +
