# Common dev commands for Musubi core.
# All targets assume `uv` is installed.

.PHONY: install fmt lint typecheck test test-cov check clean

install:
	uv sync --extra dev

fmt:
	uv run ruff format .

lint:
	uv run ruff check .

typecheck:
	uv run mypy src tests

test:
	uv run pytest

test-cov:
	uv run pytest --cov=musubi --cov-report=term-missing

check: fmt lint typecheck test
	@echo "All checks passed."

clean:
	rm -rf .pytest_cache .mypy_cache .ruff_cache build dist *.egg-info
	find . -type d -name __pycache__ -exec rm -rf {} +
