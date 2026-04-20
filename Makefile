# Common dev commands for Musubi core.
# All targets assume `uv` is installed.

.PHONY: install fmt lint typecheck test test-cov check clean \
        agent-check spec-check slice-check vault-check issue-check wikilink-check \
        tc-coverage test-integration test-integration-up test-integration-down

# --------------------------------------------------------------------------
# Code gates — ruff format + lint are scoped to the whole repo (matching
# `.github/workflows/ci.yml`'s `ruff format --check .` / `ruff check .`),
# so that _tools/*.py drift can't turn an agent's "make check green" into
# "CI red." mypy + pytest stay scoped to src/ + tests/ because those paths
# are the typed / tested modules; _tools/ are scripts that mypy-strict
# would flag for reasons unrelated to slice correctness.
# --------------------------------------------------------------------------

install:
	uv sync --extra dev

fmt:
	uv run ruff format .

lint:
	uv run ruff check .

typecheck:
	uv run mypy src tests

test:
	uv run pytest --cov=musubi --cov-report=term --cov-fail-under=85

test-cov:
	uv run pytest --cov=musubi --cov-report=term-missing --cov-fail-under=85

# Full gate for slice-worker handoff. Runs fmt (check-only) + lint + typecheck + test + coverage.
check:
	uv run ruff format --check .
	uv run ruff check .
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

# --------------------------------------------------------------------------
# Integration suite — boots the docker-compose dependency stack at
# `deploy/test-env/docker-compose.test.yml`, runs the `integration`-marked
# pytest scenarios against it, tears down with -v so volumes don't leak.
# Per slice-ops-integration-harness: GitHub Actions runs this nightly via
# `.github/workflows/integration.yml`; local devs invoke on demand.
# --------------------------------------------------------------------------

test-integration-up:
	docker compose -f deploy/test-env/docker-compose.test.yml -p musubi-integration up -d --wait

test-integration-down:
	docker compose -f deploy/test-env/docker-compose.test.yml -p musubi-integration down -v --remove-orphans

test-integration:
	@if ! command -v docker >/dev/null 2>&1; then \
	  echo "make test-integration requires docker; install Docker Desktop or skip"; \
	  exit 2; \
	fi
	uv run pytest tests/integration/ -m integration -ra --strict-markers --no-cov

clean:
	rm -rf .pytest_cache .mypy_cache .ruff_cache build dist *.egg-info
	find . -type d -name __pycache__ -exec rm -rf {} +
