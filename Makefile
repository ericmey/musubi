# Common dev commands for Musubi core.
# All targets assume `uv` is installed.

.PHONY: install fmt lint typecheck test test-cov check clean \
        agent-check spec-check slice-check vault-check issue-check wikilink-check \
        tc-coverage

# --------------------------------------------------------------------------
# Code gates — scoped to src/ + tests/ so vault tooling under
# docs/architecture/_tools/ doesn't block code-only workflows. To exercise
# the vault tooling lint, run `uv run ruff check .` explicitly.
# --------------------------------------------------------------------------

install:
	uv sync --extra dev

fmt:
	uv run ruff format src tests

lint:
	uv run ruff check src tests

typecheck:
	uv run mypy src tests

test:
	uv run pytest --cov=musubi --cov-report=term --cov-fail-under=85

test-cov:
	uv run pytest --cov=musubi --cov-report=term-missing --cov-fail-under=85

# Full gate for slice-worker handoff. Runs fmt (check-only) + lint + typecheck + test + coverage.
check:
	uv run ruff format --check src tests
	uv run ruff check src tests
	uv run mypy src tests
	uv run pytest --cov=musubi --cov-report=term --cov-fail-under=85
	@echo "All checks passed."

# --------------------------------------------------------------------------
# Vault-state gates — advertised in docs/AGENT-PROCESS.md + CLAUDE.md.
# Back them with the single source-of-truth checker at
# docs/architecture/_tools/check.py; the four target names are aliases so
# agents can reach for whichever vocabulary the spec they're reading used.
# --------------------------------------------------------------------------

agent-check:
	@python3 docs/architecture/_tools/check.py all

vault-check: agent-check

slice-check:
	@python3 docs/architecture/_tools/check.py slices

spec-check:
	@python3 docs/architecture/_tools/check.py specs

issue-check:
	@python3 docs/architecture/_tools/check.py issues

wikilink-check:
	@python3 docs/architecture/_tools/check.py wikilinks

# Mechanical audit of the Test Contract Closure Rule for one slice.
# Reads the slice file, finds the specs it implements, parses each spec's
# ## Test Contract section, classifies every bullet (passing / skipped /
# out-of-scope / missing), and emits a markdown table suitable for pasting
# into the PR template's Test Contract coverage matrix. Exits non-zero if
# any bullet is ✗ missing. Usage: `make tc-coverage SLICE=slice-plane-episodic`.
tc-coverage:
	@if [ -z "$(SLICE)" ]; then \
	  echo "usage: make tc-coverage SLICE=<slice-id>"; exit 2; \
	fi
	@python3 docs/architecture/_tools/tc_coverage.py $(SLICE)

clean:
	rm -rf .pytest_cache .mypy_cache .ruff_cache build dist *.egg-info
	find . -type d -name __pycache__ -exec rm -rf {} +
