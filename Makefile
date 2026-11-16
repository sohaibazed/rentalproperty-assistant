# Entry points for setup, checks, and each agent job.
# Secrets come from the environment; see .env.example.

PY := uv run python -m rental_agent

.PHONY: setup test lint gmail-auth sync-email sync-email-dry tax-report

setup:  ## Create .venv and install dependencies
	uv sync

test:
	uv run pytest -q

lint:
	uv run ruff check src tests

gmail-auth:  ## One-time browser consent for read-only Gmail access
	$(PY) gmail-auth

sync-email:  ## Import rent/expense emails since the last successful run
	$(PY) sync-email

sync-email-dry:  ## Show what sync-email would import, without writing anything
	$(PY) sync-email --dry-run

tax-report:  ## Year-end Schedule E summary; override with YEAR=2026
	$(PY) tax-report $(if $(YEAR),--year $(YEAR))
